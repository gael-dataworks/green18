from __future__ import annotations

import asyncio
import hashlib
import time
from contextlib import asynccontextmanager

import httpx

from logger_config import logger
from modules.scene_planner.schema import OSD
from pipeline.task import Candidate, PipelineTask
from utils.http import download_image


class StageError(Exception):
    """Raised by a pipeline stage; carries stage name + original cause."""

    def __init__(self, stage: str, cause: Exception):
        super().__init__(f"{stage}: {type(cause).__name__}: {cause}")
        self.stage = stage
        self.cause = cause


@asynccontextmanager
async def stage_guard(
    task: PipelineTask,
    stage_name: str,
    sem: asyncio.Semaphore,
    status: dict[str, str],
):
    """Acquire semaphore, log start/done, wrap exceptions in StageError."""
    async with sem:
        status[task.stem] = stage_name
        t0 = time.time()
        try:
            yield
        except Exception as exc:
            raise StageError(stage_name, exc) from exc
        finally:
            dt = time.time() - t0


async def prepare_inputs_stage(
    task: PipelineTask,
    *,
    planner,
    use_planner: bool,
    http_client: httpx.AsyncClient,
    sem: asyncio.Semaphore,
    status: dict[str, str],
) -> None:
    """Always download the reference image. Optionally run the planner.

    When `use_planner=False` (or planner=None), `task.osd` stays None and the
    coder/critic must work in pure-image mode (`actors.coder.multimodal=true`).
    """
    async with stage_guard(task, "prepare", sem, status):
        task.image_bytes, task.image_mime = await download_image(
            task.image_url, http_client
        )
        if use_planner and planner is not None:
            osd = await planner.plan(
                task_id=task.stem,
                image_bytes=task.image_bytes,
                image_url=task.image_url,
                mime=task.image_mime,
            )
            task.osd = osd.model_dump_json(indent=2)
        else:
            task.osd = None


async def code_and_check(
    task: PipelineTask,
    *,
    coder,
    js_checker,
    sem_coder: asyncio.Semaphore,
    sem_checker: asyncio.Semaphore,
    coder_multimodal: bool,
    status: dict[str, str],
    last_report=None,
    render_from_object: bool = False,
) -> None:
    """Code → JS check → retry-on-js-error loop. Returns when js_valid is True."""
    checker_mode = "with_object" if render_from_object else "sanity"
    osd = OSD.model_validate_json(task.osd) if task.osd is not None else None
    send_image = (osd is None) or coder_multimodal
    attempt = 0
    while True:
        async with stage_guard(task, "coder", sem_coder, status):
            if task.iteration == 0 and attempt == 0:
                task.js_code = await coder.code(
                    task_id=task.stem,
                    osd=osd,
                    image_bytes=task.image_bytes if send_image else None,
                    image_mime=task.image_mime,
                )
            elif last_report is not None and attempt == 0:
                task.js_code = await coder.code_critic_repair(
                    task_id=task.stem,
                    osd=osd,
                    issues=last_report.issues,
                    overall_score=last_report.overall_score,
                    matching_aspects=list(
                        getattr(last_report, "matching_aspects", []) or []
                    ),
                    image_bytes=task.image_bytes if coder_multimodal else None,
                    image_mime=task.image_mime,
                    render_png=task.rendered_png if coder_multimodal else None,
                )
            else:
                task.js_code = await coder.code_repair(
                    task_id=task.stem,
                    osd=osd,
                    js_errors=list(task.js_errors or []),
                )

        async with stage_guard(task, "js_checker", sem_checker, status):
            task.js_valid = None
            task.js_errors = []
            task.scene_json = None
            await js_checker.process(task, mode=checker_mode)

        if task.js_valid:
            return

        attempt += 1


async def renderer_stage(
    task: PipelineTask,
    *,
    renderer,
    sem: asyncio.Semaphore,
    status: dict[str, str],
) -> None:
    async with stage_guard(task, "renderer", sem, status):
        task.render_errors = []
        task.failed = False
        task.failure_reason = None

        await renderer.process(task)

        if task.failed or task.rendered_png is None:
            reason = task.failure_reason or (
                task.render_errors[0] if task.render_errors else "no png"
            )
            raise StageError("renderer", RuntimeError(reason))


async def critic_stage(
    task: PipelineTask,
    *,
    critic,
    sem: asyncio.Semaphore,
    status: dict[str, str],
):
    async with stage_guard(task, "critic", sem, status):
        if (
            task.image_bytes is None
            or task.js_code is None
            or task.rendered_png is None
        ):
            raise StageError("critic", RuntimeError("missing inputs"))

        osd = OSD.model_validate_json(task.osd) if task.osd is not None else None
        report = await critic.critique(
            task_id=task.stem,
            image_bytes=task.image_bytes,
            image_mime=task.image_mime,
            render_png=task.rendered_png,
            artifact_context={
                "kind": "coder_v1",
                "js_code": task.js_code,
                "osd": osd.model_dump() if osd is not None else None,
            },
        )
        return report


async def multigen_first_iter(
    task: PipelineTask,
    *,
    coder,
    judge,
    embedder=None,
    js_checker,
    renderer,
    session_store,
    sem_coder: asyncio.Semaphore,
    sem_checker: asyncio.Semaphore,
    sem_renderer: asyncio.Semaphore,
    sem_judge: asyncio.Semaphore,
    coder_multimodal: bool,
    status: dict[str, str],
    ensemble_size: int,
    ensemble_temperature: float,
    render_from_object: bool = False,
) -> None:
    """K-of-N generation + judge bracket. Replaces code_and_check + renderer
    on iteration 0 when `coder.ensemble_size > 1`.

    Mutates `task` so the rest of the iteration loop (critic → code_critic_repair)
    can continue on the winner.
    """

    # TODO This function should connect to stages coder and judge and js_checker and renderer
    # Bracket shoud be in judge dir and
    checker_mode = "with_object" if render_from_object else "sanity"
    osd = OSD.model_validate_json(task.osd) if task.osd is not None else None
    send_image = (osd is None) or coder_multimodal
    K = ensemble_size
    base_seed = task.seed

    logger.info(
        f"[MULTIGEN] {task.stem} K={K} | temperature={ensemble_temperature} | "
        f"multimodal={send_image} | osd={'yes' if osd else 'no'}"
    )

    # Embed the reference image once (overlaps coder generation). Shared by every
    # candidate's S2BV embeddings npz under the "prompt" key.
    ref_emb_task = (
        asyncio.create_task(embedder.embed_reference(task.image_bytes))
        if embedder is not None and judge is not None and task.image_bytes
        else None
    )
    ref_vec = None

    async def _gen(k: int) -> Candidate:
        cand = Candidate(k=k, seed=base_seed + k)
        t0 = time.time()
        try:
            async with stage_guard(task, f"coder#k{k}", sem_coder, status):
                cand.js_code = await coder.code(
                    task_id=task.stem,
                    osd=osd,
                    image_bytes=task.image_bytes if send_image else None,
                    image_mime=task.image_mime,
                    actor_override=f"coder#k{k}",
                    seed_override=cand.seed,
                    temperature_override=ensemble_temperature,
                )
        except StageError as exc:
            cand.drop_reason = f"coder:{type(exc.cause).__name__}"
        except Exception as exc:
            cand.drop_reason = f"coder:{type(exc).__name__}"
        cand.elapsed_s = time.time() - t0
        return cand

    candidates = await asyncio.gather(*[_gen(k) for k in range(K)])

    # Dedupe by SHA256(js_code)
    unique_leaders: dict[str, Candidate] = {}
    duplicates: list[Candidate] = []
    for cand in candidates:
        if cand.js_code is None:
            continue
        digest = hashlib.sha256(cand.js_code.encode("utf-8")).hexdigest()
        if digest in unique_leaders:
            cand.drop_reason = f"duplicate_of_k{unique_leaders[digest].k}"
            duplicates.append(cand)
        else:
            unique_leaders[digest] = cand

    # Validate unique candidates: js_checker + renderer, in parallel
    async def _validate(cand: Candidate) -> None:
        if cand.drop_reason is not None or cand.js_code is None:
            return
        shadow = PipelineTask(stem=f"{task.stem}#k{cand.k}", image_url=task.image_url)
        shadow.js_code = cand.js_code
        shadow.image_bytes = task.image_bytes
        shadow.image_mime = task.image_mime
        t0 = time.time()
        async with sem_checker:
            try:
                await js_checker.process(shadow, mode=checker_mode)
            except Exception as exc:
                cand.drop_reason = f"checker:{type(exc).__name__}"
                cand.elapsed_s += time.time() - t0
                return
        cand.js_valid = shadow.js_valid
        cand.js_errors = list(shadow.js_errors or [])
        cand.scene_json = shadow.scene_json
        if not shadow.js_valid:
            cand.drop_reason = "checker"
            cand.elapsed_s += time.time() - t0
            return
        async with sem_renderer:
            try:
                await renderer.process(shadow)
            except Exception as exc:
                cand.drop_reason = f"renderer:{type(exc).__name__}"
                cand.elapsed_s += time.time() - t0
                return
        cand.rendered_png = shadow.rendered_png
        task.multigen_pngs.append(shadow.rendered_png)
        cand.render_errors = list(shadow.render_errors or [])
        if not shadow.rendered_png:
            cand.drop_reason = "renderer"
        cand.elapsed_s += time.time() - t0

        # Extra per-angle views for the multi-stage judge. Best-effort: failures
        # leave the dicts empty and the judge falls back to grid-only stages.
        if (
            cand.drop_reason is None
            and judge is not None
            and cand.js_code
            and getattr(renderer, "config", None) is not None
            and getattr(renderer.config, "judge_multiview", False)
            and hasattr(renderer, "render_judge_views")
        ):
            async with sem_renderer:
                cand.judge_white_views, cand.judge_gray_views = (
                    await renderer.render_judge_views(shadow.stem, cand.js_code)
                )

            # DINOv3 embeddings for judge stage S2BV (best-view). Best-effort:
            # None keeps S2BV disabled for this candidate.
            if embedder is not None and ref_vec is not None and cand.judge_white_views:
                cand.judge_embeddings = await embedder.build_candidate_npz(
                    ref_vec, cand.judge_white_views
                )

    if ref_emb_task is not None:
        ref_vec = await ref_emb_task

    await asyncio.gather(*[_validate(c) for c in unique_leaders.values()])

    # Mirror validation results onto duplicates (they share js_code so they
    # render identically; this keeps logging meaningful).
    for dup in duplicates:
        digest = hashlib.sha256(dup.js_code.encode("utf-8")).hexdigest()
        leader = unique_leaders[digest]
        dup.js_valid = leader.js_valid
        dup.js_errors = list(leader.js_errors)
        dup.scene_json = leader.scene_json
        dup.rendered_png = leader.rendered_png
        dup.render_errors = list(leader.render_errors)

    task.candidates = list(candidates)

    survivors = [
        c for c in candidates
        if c.drop_reason is None and c.rendered_png is not None
    ]
    drops = [c.drop_reason for c in candidates]
    if not survivors:
        logger.warning(
            f"[MULTIGEN] {task.stem} K={K} | drops={drops} | ALL CANDIDATES FAILED"
        )
        raise StageError("multigen", RuntimeError("all candidates failed"))

    logger.info(
        f"[MULTIGEN] {task.stem} K={K} | unique={len(unique_leaders)} | "
        f"survivors={[c.k for c in survivors]} | drops={drops}"
    )

    # Judge bracket — single-elimination pairwise.
    winner = await _run_bracket(
        task=task, survivors=survivors, judge=judge, sem_judge=sem_judge,
    )

    # Promote winner into task.
    task.winner_k = winner.k
    task.js_code = winner.js_code
    task.js_valid = winner.js_valid
    task.js_errors = list(winner.js_errors)
    task.scene_json = winner.scene_json
    task.rendered_png = winner.rendered_png

    # Rename winner's coder session to the canonical "coder" actor so that
    # downstream code_critic_repair / code_repair calls find it.
    session_store.rename_actor(task.stem, f"coder#k{winner.k}", "coder")
    # Evict loser sessions to free memory.
    for cand in candidates:
        if cand.k != winner.k:
            session_store.evict_actor(task.stem, f"coder#k{cand.k}")

    logger.info(
        f"[MULTIGEN] {task.stem} winner=k{winner.k} | "
        f"js_bytes={len(winner.js_code.encode('utf-8')) if winner.js_code else 0}"
    )


async def _run_bracket(
    *,
    task: PipelineTask,
    survivors: list[Candidate],
    judge,
    sem_judge: asyncio.Semaphore,
) -> Candidate:
    """Single-elimination bracket via pairwise Judge calls."""
    if len(survivors) == 1:
        return survivors[0]
    if judge is None:
        logger.warning(
            f"[BRACKET] {task.stem} | judge is None — falling back to k{survivors[0].k}"
        )
        return survivors[0]

    round_idx = 1
    current = list(survivors)
    while len(current) > 1:
        pairs: list[tuple[Candidate, Candidate]] = []
        byes: list[Candidate] = []
        for i in range(0, len(current), 2):
            if i + 1 < len(current):
                pairs.append((current[i], current[i + 1]))
            else:
                byes.append(current[i])

        async def _match(pair_idx: int, a: Candidate, b: Candidate) -> Candidate:
            label = f"R{round_idx}M{pair_idx + 1} k{a.k}-vs-k{b.k}"
            async with sem_judge:
                verdict = await judge.compare(
                    task_id=task.stem,
                    match_label=label,
                    reference_bytes=task.image_bytes,
                    reference_mime=task.image_mime,
                    render_a=a.rendered_png,
                    render_b=b.rendered_png,
                    white_views_a=a.judge_white_views,
                    white_views_b=b.judge_white_views,
                    gray_views_a=a.judge_gray_views,
                    gray_views_b=b.judge_gray_views,
                    embeddings_a=a.judge_embeddings,
                    embeddings_b=b.judge_embeddings,
                )
            return a if verdict.winner == "A" else b

        results = await asyncio.gather(*[
            _match(idx, a, b) for idx, (a, b) in enumerate(pairs)
        ])
        current = list(results) + byes
        logger.info(
            f"[BRACKET R{round_idx}] {task.stem} | matches={len(pairs)} | "
            f"byes={[c.k for c in byes]} | advancing={[c.k for c in current]}"
        )
        round_idx += 1
    return current[0]

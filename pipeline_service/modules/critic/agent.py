from __future__ import annotations

import base64
import json
import re
import time
from typing import Any

from config.settings import ProviderRoutingConfig
from logger_config import logger
from modules.base_agent import BaseAgent
from modules.critic.prompts import CRITIC_SYSTEM_PROMPT, CRITIC_USER_TEMPLATE
from modules.critic.schema import CriticReport
from utils.json_extract import extract_json_object
from utils.retry import async_retry
from config.settings import ActorConfig

_ARTIFACT_INLINE_MAX_CHARS = 4000

_JS_MESH_ID_RE = re.compile(
    r"\bconst\s+(\w+)\s*=\s*new\s+THREE\."
    r"(?:Mesh|InstancedMesh|Group|LineSegments|Points)\b"
)
_JS_GEOM_ID_RE = re.compile(
    r"\bconst\s+(\w+(?:Geom|Geo|Geometry))\s*=\s*new\s+THREE\.(\w+)Geometry\b"
)


def _extract_js_part_names(js_code: str, limit: int = 30) -> list[dict[str, str]]:
    """Pull mesh/geometry identifiers out of the coder's JS module."""
    if not isinstance(js_code, str) or not js_code:
        return []
    mesh_names = [m.group(1) for m in _JS_MESH_ID_RE.finditer(js_code)]
    geom_by_var: dict[str, str] = {}
    for m in _JS_GEOM_ID_RE.finditer(js_code):
        geom_by_var[m.group(1)] = m.group(2)
    out: list[dict[str, str]] = []
    seen: set[str] = set()
    for name in mesh_names:
        if name in seen:
            continue
        seen.add(name)
        geo = next(
            (gt for gv, gt in geom_by_var.items() if gv.lower().startswith(name.lower())),
            "",
        )
        out.append({"id": name, "geometry": geo})
        if len(out) >= limit:
            break
    return out


def _summarize_artifact_context(artifact_context: dict) -> str:
    """Render artifact context JSON, truncated if too long."""
    if artifact_context.get("kind") == "coder_v1":
        js_code = artifact_context.get("js_code") or ""
        osd = artifact_context.get("osd")
        compact: dict[str, Any] = {
            "kind": artifact_context.get("kind"),
            "js_parts": _extract_js_part_names(js_code),
        }
        if osd is not None:
            compact["osd"] = osd
        else:
            compact["mode"] = "pure-image (no OSD)"
        full = json.dumps(compact, indent=2)
        return full[:_ARTIFACT_INLINE_MAX_CHARS]

    full = json.dumps(artifact_context, indent=2)
    return full[:_ARTIFACT_INLINE_MAX_CHARS]


def critic_report_schema_prompt() -> str:
    """Expose the Critic system prompt for external callers (tests, docs)."""
    return CRITIC_SYSTEM_PROMPT


class CriticAgent(BaseAgent):
    """Stateless visual critic agent. One call ↔ one CriticReport.

    Holds config (`client`, `model`, `max_tokens`, `seed`, `reasoning_effort`,
    `ensemble_size`) so the orchestrator can call `await critic.critique(...)`
    the same way it calls `planner.plan(...)` / `coder.code(...)`. No session
    state — each call is independent (Qwen-VL sees a cold context every time).
    """
    actor = "critic"
    def __init__(
        self,
        client: AsyncOpenAI,
        settings: ActorConfig,
        *,
        max_retries: int = 2,
    ) -> None:
        super().__init__(client, settings)
        self.max_retries = max_retries
        self.reasoning_effort = settings.reasoning_effort
        self.ensemble_size = settings.ensemble_size

    async def critique(
        self,
        *,
        task_id: str,
        image_bytes: bytes,
        image_mime: str,
        render_png: bytes,
        artifact_context: dict,
    ) -> CriticReport:
        return await run_critic(
            task_id=task_id,
            image_bytes=image_bytes,
            image_mime=image_mime,
            render_png=render_png,
            artifact_context=artifact_context,
            client=self.client,
            model=self.model,
            max_tokens=self.max_tokens,
            temperature=self.temperature,
            top_p=self.top_p,
            top_k=self.top_k,
            min_p=self.min_p,
            presence_penalty=self.presence_penalty,
            repetition_penalty=self.repetition_penalty,
            seed=self.seed,
            max_retries=self.max_retries,
            reasoning_effort=self.reasoning_effort,
            ensemble_size=self.ensemble_size,
            backend=self.backend,
            providers=self.providers,
        )


async def run_critic(
    *,
    task_id: str,
    image_bytes: bytes,
    image_mime: str,
    render_png: bytes,
    artifact_context: dict,
    client: Any,
    model: str,
    max_tokens: int = 4096,
    temperature: float = 0.0,
    top_p: float | None = None,
    top_k: int | None = None,
    min_p: float | None = None,
    presence_penalty: float | None = None,
    repetition_penalty: float | None = None,
    seed: int | None = 42,
    max_retries: int = 2,
    reasoning_effort: str | None = None,
    ensemble_size: int = 1,
    backend: str = "openrouter",
    providers: ProviderRoutingConfig | None = None,
) -> CriticReport:
    """Run one Critic call. Returns a validated CriticReport."""
    logger.info(
        f"[5/7 Critic] Started Task {task_id} | Model: {model} | Image KB: {len(image_bytes) / 1024:.1f} | Render KB: {len(render_png) / 1024:.1f} | Artifact Context: {len(artifact_context)}"
    )

    image_b64 = base64.b64encode(image_bytes).decode()
    render_b64 = base64.b64encode(render_png).decode()
    artifact_json = _summarize_artifact_context(artifact_context)
    extra_body: dict[str, Any] = {}
    if reasoning_effort:
        if backend == "vllm":
            extra_body["chat_template_kwargs"] = {"enable_thinking": True}
        else:
            extra_body["reasoning"] = {"effort": reasoning_effort}
            
    for _ek, _ev in (
        ("top_k", top_k),
        ("min_p", min_p),
        ("repetition_penalty", repetition_penalty),
    ):
        if _ev is not None:
            extra_body[_ek] = _ev

    call_kwargs: dict[str, Any] = {}
    if top_p is not None:
        call_kwargs["top_p"] = top_p
    if presence_penalty is not None:
        call_kwargs["presence_penalty"] = presence_penalty
    if providers is not None and backend != "vllm":
        extra_body["provider"] = providers.model_dump(exclude_none=True)

    async def _one_call(call_seed: int | None) -> CriticReport:
        async def _call(_attempt: int, _last_err: str | None) -> CriticReport:
            t0 = time.time()
            response = await client.chat.completions.create(
                model=model,
                messages=[
                    {"role": "system", "content": CRITIC_SYSTEM_PROMPT},
                    {
                        "role": "user",
                        "content": [
                            {"type": "text", "text": CRITIC_USER_TEMPLATE.format(scene_ir_json=artifact_json)},
                            {"type": "image_url", "image_url": {"url": f"data:{image_mime};base64,{image_b64}"}},
                            {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{render_b64}"}},
                        ],
                    },
                ],
                temperature=temperature,
                seed=call_seed,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
                extra_body=extra_body or None,
                **call_kwargs,
            )
            if not response.choices or not response.choices[0].message.content:
                raise ValueError("Critic returned empty response")
            usage = getattr(response, "usage", None)
            if usage is not None:
                logger.debug(
                    f"[TOKENS Actor: critic Task:{task_id}] | "
                    f"Prompt: {usage.prompt_tokens} | Completion: {usage.completion_tokens} | "
                    f"Total: {usage.total_tokens} | Finish Reason: {response.choices[0].finish_reason} | "
                    f"Max Tokens Cap: {max_tokens}"
                )
            raw = response.choices[0].message.content.strip()
            report = CriticReport.model_validate_json(extract_json_object(raw))
            logger.info(
                f"[5/7 Critic] Finished Task {task_id} | Elapsed: {time.time() - t0:.1f}s | Score: {report.overall_score:.2f} | Issues: {len(report.issues)} | Seed: {call_seed}"
            )
            return report

        return await async_retry(_call, max_retries=max_retries)

    if ensemble_size <= 1:
        return await _one_call(seed)

    import asyncio as _asyncio
    seeds = [seed if seed is None else seed + i for i in range(ensemble_size)]
    reports = await _asyncio.gather(*[_one_call(s) for s in seeds])

    sorted_by_score = sorted(reports, key=lambda r: r.overall_score)
    median_report = sorted_by_score[len(sorted_by_score) // 2]
    mean_score = sum(r.overall_score for r in reports) / len(reports)
    union_matching: list[str] = []
    seen: set[str] = set()
    for r in reports:
        for m in r.matching_aspects:
            key = m.strip().lower()
            if key not in seen:
                seen.add(key)
                union_matching.append(m)

    merged = median_report.model_copy(update={
        "overall_score": mean_score,
        "matching_aspects": union_matching,
    })
    logger.info(
        f"[5/7 Critic] Finished Task {task_id} | Ensemble: {ensemble_size} | Scores: {[round(r.overall_score, 2) for r in reports]} | Mean: {mean_score:.2f} | Median: {median_report.overall_score:.2f} | Issues: {len(merged.issues)}"
    )
    return merged

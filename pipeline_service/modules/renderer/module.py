from __future__ import annotations

import asyncio
import base64
import os
import time
from dataclasses import dataclass, field
from pathlib import Path

import httpx

from logger_config import logger
from modules.base import BaseModule
from modules.renderer.settings import RendererConfig
from pipeline.task import PipelineTask

_RUNNER_JS = Path(__file__).parent / "render_service" / "render_runner.mjs"


@dataclass
class _Sidecar:
    idx: int
    port: int
    proc: asyncio.subprocess.Process
    client: httpx.AsyncClient
    log_tasks: list[asyncio.Task] = field(default_factory=list)


class RendererModule(BaseModule):
    _WATCHDOG_INTERVAL_S = 5.0
    _PING_FAILS_BEFORE_RESPAWN = 3

    def __init__(self, config: RendererConfig) -> None:
        self.config = config
        self._sidecars: list[_Sidecar] = []
        self._dispatch_counter = 0
        self._dispatch_lock = asyncio.Lock()
        self._node_cwd: str | None = None
        self._respawning: set[int] = set()
        self._ping_fails: dict[int, int] = {}
        self._watchdog_task: asyncio.Task | None = None
        self._shutting_down = False

    async def startup(self) -> None:
        if not _RUNNER_JS.exists():
            logger.warning(f"[RENDERER] runner missing at {_RUNNER_JS}, disabling")
            return

        self._node_cwd = self._resolve_node_cwd()
        count = max(1, self.config.sidecar_count)
        logger.info(
            f"[RENDERER] starting {count} sidecar(s) | node={self.config.node_binary} "
            f"runner={_RUNNER_JS} base_port={self.config.sidecar_port} cwd={self._node_cwd} "
            f"pool_size_per_sidecar={self.config.pool_size}"
        )

        try:
            self._sidecars = await asyncio.gather(
                *[self._spawn_sidecar(i) for i in range(count)]
            )
        except Exception:
            await self.shutdown()
            raise

        logger.info(
            f"[RENDERER] {len(self._sidecars)} sidecar(s) ready, total slots="
            f"{len(self._sidecars) * self.config.pool_size}"
        )

        self._watchdog_task = asyncio.create_task(self._watchdog())

    async def _spawn_sidecar(self, idx: int) -> _Sidecar:
        port = self.config.sidecar_port + idx
        static_port = self.config.static_port_base + idx
        env = {
            **os.environ,
            "PORT": str(port),
            "STATIC_PORT": str(static_port),
            "RENDER_POOL_SIZE": str(self.config.pool_size),
            "RENDER_TIMEOUT_MS": str(self.config.render_timeout_ms),
            "PROTOCOL_TIMEOUT_MS": str(self.config.protocol_timeout_ms),
        }
        proc = await asyncio.create_subprocess_exec(
            self.config.node_binary,
            str(_RUNNER_JS),
            env=env,
            cwd=self._node_cwd,
            stdout=asyncio.subprocess.PIPE,
            stderr=asyncio.subprocess.PIPE,
        )
        stdout_task = asyncio.create_task(
            self._pipe_logger(proc.stdout, f"#{idx}/stdout")
        )
        stderr_task = asyncio.create_task(
            self._pipe_logger(proc.stderr, f"#{idx}/stderr")
        )
        client = httpx.AsyncClient(timeout=self.config.request_timeout_s)
        await self._wait_ready_for(proc, client, port, idx)
        logger.info(f"[RENDERER] sidecar #{idx} ready on port {port}")
        return _Sidecar(idx=idx, port=port, proc=proc, client=client,
                        log_tasks=[stdout_task, stderr_task])

    async def _watchdog(self) -> None:
        try:
            while not self._shutting_down:
                await asyncio.sleep(self._WATCHDOG_INTERVAL_S)
                for sc in list(self._sidecars):
                    if self._shutting_down:
                        break
                    if sc.idx in self._respawning:
                        continue
                    if not await self._sidecar_alive(sc):
                        asyncio.create_task(self._respawn(sc.idx))
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(f"[RENDERER] watchdog error: {exc}")

    async def _sidecar_alive(self, sc: _Sidecar) -> bool:
        if sc.proc.returncode is not None:
            logger.warning(
                f"[RENDERER] sidecar #{sc.idx} process exited (rc={sc.proc.returncode})"
            )
            return False
        ping_url = f"http://{self.config.sidecar_host}:{sc.port}/ping"
        try:
            r = await sc.client.get(ping_url, timeout=2.0)
            ok = r.status_code == 200
        except Exception:
            ok = False
        if ok:
            self._ping_fails[sc.idx] = 0
            return True
        fails = self._ping_fails.get(sc.idx, 0) + 1
        self._ping_fails[sc.idx] = fails
        if fails >= self._PING_FAILS_BEFORE_RESPAWN:
            logger.warning(
                f"[RENDERER] sidecar #{sc.idx} failed {fails} consecutive pings"
            )
            return False
        return True

    async def _respawn(self, idx: int) -> None:
        if idx in self._respawning or self._shutting_down:
            return
        self._respawning.add(idx)
        try:
            logger.info(f"[RENDERER] respawning sidecar #{idx}")
            old = next((s for s in self._sidecars if s.idx == idx), None)
            if old is not None:
                await self._teardown_sidecar(old)
            new_sc = await self._spawn_sidecar(idx)
            self._ping_fails[idx] = 0
            for pos, s in enumerate(self._sidecars):
                if s.idx == idx:
                    self._sidecars[pos] = new_sc
                    break
            else:
                self._sidecars.append(new_sc)
            logger.info(f"[RENDERER] sidecar #{idx} respawned")
        except Exception as exc:
            logger.warning(f"[RENDERER] sidecar #{idx} respawn failed: {exc}")
        finally:
            self._respawning.discard(idx)

    async def _teardown_sidecar(self, sc: _Sidecar) -> None:
        try:
            await sc.client.aclose()
        except Exception:
            pass
        if sc.proc.returncode is None:
            try:
                sc.proc.terminate()
                try:
                    await asyncio.wait_for(sc.proc.wait(), timeout=5.0)
                except asyncio.TimeoutError:
                    sc.proc.kill()
                    await sc.proc.wait()
            except ProcessLookupError:
                pass
        for t in sc.log_tasks:
            if not t.done():
                t.cancel()
                try:
                    await t
                except (asyncio.CancelledError, Exception):
                    pass

    async def shutdown(self) -> None:
        self._shutting_down = True
        if self._watchdog_task is not None:
            self._watchdog_task.cancel()
            try:
                await self._watchdog_task
            except (asyncio.CancelledError, Exception):
                pass
            self._watchdog_task = None

        sidecars = self._sidecars
        self._sidecars = []

        for sc in sidecars:
            try:
                await sc.client.aclose()
            except Exception:
                pass

        for sc in sidecars:
            if sc.proc.returncode is None:
                logger.info(f"[RENDERER] terminating sidecar #{sc.idx}")
                try:
                    sc.proc.terminate()
                    try:
                        await asyncio.wait_for(sc.proc.wait(), timeout=5.0)
                    except asyncio.TimeoutError:
                        logger.warning(
                            f"[RENDERER] sidecar #{sc.idx} did not exit on SIGTERM, sending SIGKILL"
                        )
                        sc.proc.kill()
                        await sc.proc.wait()
                except ProcessLookupError:
                    pass

        for sc in sidecars:
            for t in sc.log_tasks:
                if not t.done():
                    t.cancel()
                    try:
                        await t
                    except (asyncio.CancelledError, Exception):
                        pass

    async def _next_sidecar(self) -> _Sidecar | None:
        async with self._dispatch_lock:
            n = len(self._sidecars)
            if n == 0:
                return None
            for _ in range(n):
                sc = self._sidecars[self._dispatch_counter % len(self._sidecars)]
                self._dispatch_counter += 1
                if sc.idx not in self._respawning:
                    return sc
            return None

    async def process(self, task: PipelineTask) -> PipelineTask:
        if task.failed or (not task.js_code and not task.scene_json):
            logger.debug(
                f"[RENDERER] '{task.stem}' skip "
                f"(failed={task.failed}, has_code={bool(task.js_code)}, "
                f"has_scene={bool(task.scene_json)})"
            )
            return task

        sc = await self._next_sidecar()
        if sc is None:
            task.render_errors = ["no sidecars available"]
            logger.warning(f"[RENDERER] '{task.stem}' skip — no sidecars available")
            return task

        use_object = task.scene_json is not None

        options = {
            "imgSize": self.config.img_size,
            "gap": self.config.grid_gap,
        }
        if self.config.bg_color:
            options["bgColor"] = self.config.bg_color

        if use_object:
            payload = {"object": task.scene_json, "options": options}
            endpoint = "/render/object"
            size_note = "scene_json"
        else:
            payload = {"source": task.js_code, "options": options}
            endpoint = "/render/grid"
            size_note = f"js_code={len(task.js_code)} bytes"

        logger.info(
            f"[RENDERER] '{task.stem}' start | sidecar=#{sc.idx} "
            f"| path={'object' if use_object else 'code'} | {size_note}"
        )

        url = f"http://{self.config.sidecar_host}:{sc.port}{endpoint}"
        t0 = time.monotonic()
        try:
            resp = await sc.client.post(url, json=payload)
        except Exception as exc:
            task.render_errors = [f"{type(exc).__name__}: {exc}"]
            logger.warning(
                f"[RENDERER] '{task.stem}' FAIL (http) sidecar=#{sc.idx} | "
                f"{task.render_errors[0]}"
            )
            return task

        task.render_ms = (time.monotonic() - t0) * 1000.0

        if resp.status_code == 200:
            task.rendered_png = resp.content
            task.refinement_rendered_pngs.append(resp.content)
            logger.info(
                f"[RENDERER] '{task.stem}' PASS sidecar=#{sc.idx} | "
                f"png={len(task.rendered_png)}B render={task.render_ms/1000:.1f}s"
            )
        else:
            detail = resp.text[:200] if resp.text else ""
            task.render_errors = [f"HTTP {resp.status_code}: {detail}"]
            logger.warning(
                f"[RENDERER] '{task.stem}' FAIL (status) sidecar=#{sc.idx} | "
                f"{task.render_errors[0]} | render={task.render_ms/1000:.1f}s"
            )
            return task

        return task

    # Per-angle views consumed by the multi-stage judge.
    _JUDGE_WHITE_VIEWS = (
        "front_left", "front_right", "front_below", "front_above",
        "right", "back", "left", "top_down",
    )
    _JUDGE_GRAY_VIEWS = ("front_left",)

    async def _post_views(
        self, sc: "_Sidecar", source: str, views: tuple[str, ...], bg_color: str | None
    ) -> dict[str, bytes]:
        options: dict = {"imgSize": self.config.img_size}
        if bg_color:
            options["bgColor"] = bg_color
        url = f"http://{self.config.sidecar_host}:{sc.port}/render/views"
        resp = await sc.client.post(url, json={"source": source, "views": list(views), "options": options})
        if resp.status_code != 200:
            raise RuntimeError(f"HTTP {resp.status_code}: {resp.text[:160]}")
        payload = resp.json()
        out: dict[str, bytes] = {}
        for name, b64 in (payload.get("views") or {}).items():
            try:
                out[name] = base64.b64decode(b64)
            except Exception as exc:  # noqa: BLE001 - skip a single bad view, keep the rest
                logger.warning(f"[RENDERER] judge view {name!r} decode failed: {exc}")
        return out

    async def render_judge_views(
        self, stem: str, source: str
    ) -> tuple[dict[str, bytes], dict[str, bytes]]:
        """Render the per-angle white views + gray front view the judge consumes.

        Returns ``(white_views, gray_views)`` keyed by view name. On any failure
        returns whatever was rendered (possibly empty) so the judge degrades to
        grid-only stages rather than failing the duel.
        """
        if not source:
            return {}, {}
        sc = await self._next_sidecar()
        if sc is None:
            logger.warning(f"[RENDERER] '{stem}' judge views skipped — no sidecars available")
            return {}, {}

        white: dict[str, bytes] = {}
        gray: dict[str, bytes] = {}
        t0 = time.monotonic()
        try:
            white = await self._post_views(sc, source, self._JUDGE_WHITE_VIEWS, self.config.judge_white_bg)
        except Exception as exc:  # noqa: BLE001 - judge degrades on render failure
            logger.warning(f"[RENDERER] '{stem}' judge white views failed: {exc}")
        try:
            gray = await self._post_views(sc, source, self._JUDGE_GRAY_VIEWS, self.config.judge_gray_bg)
        except Exception as exc:  # noqa: BLE001 - S3 gray rescue self-skips when absent
            logger.warning(f"[RENDERER] '{stem}' judge gray views failed: {exc}")
        logger.info(
            f"[RENDERER] '{stem}' judge views | white={len(white)} gray={len(gray)} "
            f"| {(time.monotonic() - t0):.1f}s"
        )
        return white, gray

    def _resolve_node_cwd(self) -> str:
        preferred = os.environ.get("NODE_CWD")
        candidates = [preferred, "/workspace", str(_RUNNER_JS.parent.parent.parent.parent)]
        for c in candidates:
            if c and os.path.isdir(os.path.join(c, "node_modules")):
                return c
        return str(_RUNNER_JS.parent)

    async def _wait_ready_for(
        self,
        proc: asyncio.subprocess.Process,
        client: httpx.AsyncClient,
        port: int,
        idx: int,
    ) -> None:
        deadline = time.monotonic() + self.config.startup_timeout_s
        ping_url = f"http://{self.config.sidecar_host}:{port}/ping"
        last_err: str = ""
        while time.monotonic() < deadline:
            if proc.returncode is not None:
                raise RuntimeError(
                    f"[RENDERER] sidecar #{idx} exited early: rc={proc.returncode}"
                )
            try:
                r = await client.get(ping_url, timeout=2.0)
                if r.status_code == 200:
                    return
                last_err = f"status={r.status_code}"
            except Exception as exc:
                last_err = f"{type(exc).__name__}: {exc}"
            await asyncio.sleep(1.0)
        raise RuntimeError(
            f"[RENDERER] sidecar #{idx} not ready after {self.config.startup_timeout_s}s "
            f"(last: {last_err})"
        )

    @staticmethod
    async def _pipe_logger(stream: asyncio.StreamReader | None, which: str) -> None:
        if stream is None:
            return
        try:
            while True:
                line = await stream.readline()
                if not line:
                    break
                text = line.decode("utf-8", errors="replace").rstrip()
                if text:
                    if "stderr" in which:
                        logger.warning(f"[renderer-sidecar {which}] {text}")
                    else:
                        logger.debug(f"[renderer-sidecar {which}] {text}")
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.debug(f"[renderer-sidecar {which}] pipe error: {exc}")

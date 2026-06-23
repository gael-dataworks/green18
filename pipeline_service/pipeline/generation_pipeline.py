from __future__ import annotations

import asyncio
import gc
import os

import httpx
from openai import AsyncOpenAI

from config.settings import LLMClientConfig, SettingsConf
from logger_config import logger
from modules.js_checker.module import JSCheckerModule
from modules.renderer.module import RendererModule
from pipeline.factory import build_pipeline
from pipeline.orchestrator import Pipeline
from pipeline.state import MinerState
from pipeline.task import PipelineTask

_PLACEHOLDER_KEYS = {"", "placeholder", "your-key-here", "sk-or-...", "changeme"}
_LOCAL_HOSTS = ("localhost", "127.0.0.1", "0.0.0.0")


def _is_local_endpoint(base_url: str) -> bool:
    return any(h in base_url for h in _LOCAL_HOSTS)


def _resolve_api_key(name: str, cfg: LLMClientConfig) -> str | None:
    """Resolve api key for one client"""
    raw = os.environ.get(cfg.api_key_env, "") if cfg.api_key_env else ""
    api_key = raw.strip().strip('"').strip("'")
    if not api_key:
        api_key = cfg.api_key.strip()

    if _is_local_endpoint(cfg.base_url):
        return api_key or "local"

    if api_key.lower() in _PLACEHOLDER_KEYS:
        env_hint = f" (env var {cfg.api_key_env})" if cfg.api_key_env else ""
        logger.warning(
            f"Skipping LLM client {name!r}{env_hint}: placeholder/empty API key "
        )
        return None
    return api_key


class GenerationPipeline:
    """Top-level pipeline driver. Constructed once per app lifecycle."""

    def __init__(self, settings: SettingsConf, state: MinerState) -> None:
        self.settings = settings
        self.state = state

        # DET modules
        self.js_checker = JSCheckerModule(settings.js_checker)
        self.renderer = RendererModule(settings.renderer)

        self._clients: dict[str, AsyncOpenAI] = {}
        self._http_client: httpx.AsyncClient | None = None
        self._pipeline: Pipeline | None = None

    # Lifecycle

    async def startup(self) -> None:
        logger.info("GenerationPipeline starting")

        for name, cfg in self.settings.llm_clients.items():
            if not cfg.enabled:
                continue
            api_key = _resolve_api_key(name, cfg)
            if api_key is None:
                continue
            self._clients[name] = AsyncOpenAI(
                base_url=cfg.base_url,
                api_key=api_key,
                timeout=httpx.Timeout(900.0, connect=10.0),
                max_retries=0,
            )
            logger.info(
                f"llm client ready | name={name} base_url={cfg.base_url} "
                f"local={_is_local_endpoint(cfg.base_url)}"
            )
        limits = httpx.Limits(max_connections=200, max_keepalive_connections=50)
        self._http_client = httpx.AsyncClient(timeout=30.0, limits=limits)

        await self.js_checker.startup()
        await self.renderer.startup()
        self._pipeline = build_pipeline(
            settings=self.settings,
            clients=self._clients,
            js_checker=self.js_checker,
            renderer=self.renderer,
            http_client=self._http_client,
        )
        await self._pipeline.start()
        actors = self.settings.actors
        eb = self.settings.event_bus
        use_planner = self.settings.pipeline.use_planner
        logger.info(f"[Pipeline initialized — waiting for models]")
        if use_planner:
            logger.info(f"Planner: {actors.planner.client} | Model: {actors.planner.model}")
        else:
            logger.info("Planner: DISABLED (pipeline.use_planner=false — pure-image coder)")
        logger.info(
            f"Coder: {actors.coder.client} | Model: {actors.coder.model} | "
            f"multimodal={actors.coder.multimodal} | ensemble_size={actors.coder.ensemble_size} | "
            f"ensemble_temperature={actors.coder.ensemble_temperature}"
        )
        logger.info(f"Critic: {actors.critic.client} | Model: {actors.critic.model}")
        if actors.coder.ensemble_size > 1:
            logger.info(f"Judge: {actors.judge.client} | Model: {actors.judge.model}")
        else:
            logger.info("Judge: DISABLED (ensemble_size=1)")
        logger.info(f"Iter cap: {eb.max_iter} | Deadline: {eb.task_deadline_s:.0f}s | Threshold: {eb.score_threshold:.2f}")

    async def shutdown(self) -> None:
        logger.info("[Pipeline shutting down]")
        if self._pipeline is not None:
            await self._pipeline.stop()
        await self.renderer.shutdown()
        await self.js_checker.shutdown()
        if self._http_client is not None:
            await self._http_client.aclose()
        for name, client in list(self._clients.items()):
            try:
                await client.close()
            except Exception as exc:
                logger.warning(f"[Pipeline shutdown] Client {name} close failed: {exc}")

    def _cleanup_batch_memory(self, context: str) -> None:
        if self._pipeline is None:
            return
        sessions = self._pipeline.session_store.clear()
        statuses = len(self._pipeline.task_status)
        self._pipeline.task_status.clear()
        collected = gc.collect()
        logger.info(
            f"[{context} cleanup] sessions={sessions} "
            f"task_status={statuses} gc_collected={collected}"
        )

    async def run_batch(self, tasks: list[PipelineTask]) -> None:
        if self._pipeline is None:
            logger.error("[Batch failed] Run called before startup")
            return
        if tasks:
            batch_seed = tasks[0].seed
            if self._pipeline.planner is not None:
                self._pipeline.planner.seed = batch_seed
            self._pipeline.coder.seed = batch_seed
            self._pipeline.critic.seed = batch_seed
            if self._pipeline.judge is not None:
                self._pipeline.judge.seed = batch_seed
            planner_seed = self._pipeline.planner.seed if self._pipeline.planner is not None else "n/a"
            judge_seed = self._pipeline.judge.seed if self._pipeline.judge is not None else "n/a"
            logger.info(
                f"[Batch seed] Agents updated | seed={batch_seed} "
                f"(planner={planner_seed}, coder={self._pipeline.coder.seed}, "
                f"critic={self._pipeline.critic.seed}, judge={judge_seed})"
            )
            
        budget = self.settings.pipeline.batch_time_budget

        async def run_one(task: PipelineTask) -> None:
            while True:
                result = await (await self._pipeline.submit(task))
                if not result.failed or task.attempt ==1:
                    self.state.record_task(result)
                    return
                task = PipelineTask(
                    stem=task.stem, image_url=task.image_url, seed=task.seed,
                )
                task.attempt = result.attempt + 1
                logger.info(f"[Batch retry] {task.stem} | attempt={task.attempt}")

        try:
            logger.info(f"[Batch starting] {len(tasks)} tasks | budget={budget:.0f}s")
            await asyncio.wait_for(
                asyncio.gather(*(run_one(t) for t in tasks)),
                timeout=budget-120,
            )
            
        except asyncio.TimeoutError:
            logger.warning(f"[Batch deadline] hit {budget:.0f}s")
            for task in tasks:
                recorded = self.state.tasks.get(task.stem)
                if recorded is None or recorded.failed or not recorded.js_code:
                    task.failed = True
                    task.failure_reason = f"batch budget exceeded ({budget:.0f}s)"
                    task.failure_stage = "batch_deadline"
                    self.state.record_task(task)
            
        except asyncio.CancelledError:
            raise
        finally:
            self._cleanup_batch_memory("Batch")
            logger.info(
                f"[Batch done] {len(self.state.results)} ok, "
                f"{len(self.state.failed)} failed"
            )
    
    async def run_warmup(self, task: PipelineTask) -> None:
        if self._pipeline is None:
            logger.error("[Warmup failed] Run called before startup")
            return
        try:
            logger.info(f"[Warmup task starting]")
            future = await self._pipeline.submit(task)
            await future
            logger.info(f"[Warmup task completed]")
        except asyncio.CancelledError:
            logger.info("[Warmup task cancelled]")
            raise
        except Exception as exc:
            logger.exception(f"[Warmup failed] {exc}")
        finally:
            self._cleanup_batch_memory("Warmup")

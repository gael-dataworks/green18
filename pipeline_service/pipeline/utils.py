from __future__ import annotations

import gc
import os
import signal
import subprocess
import time

import requests
from loguru import logger

from modules.metrics.gpu import resolve_vllm_gpu_config

MAX_OOM_RETRIES = 3


def _kill_vllm_by_port(port: int) -> None:
    try:
        result = subprocess.check_output(["fuser", f"{port}/tcp"], text=True).strip()
        for pid in result.split():
            os.kill(int(pid), signal.SIGTERM)
            logger.warning(f"[OOM] killed vllm pid={pid} port={port}")
    except subprocess.CalledProcessError:
        logger.warning(f"[OOM] no process found on port {port}")
    except Exception as e:
        logger.error(f"[OOM] failed to kill vllm on port {port}: {e}")


def _start_vllm(port: int, model: str, api_key: str, max_model_len: int,
                tensor_parallel_size: int, gpu_memory_utilization: float,
                max_num_seqs: int, gpu_ids: str) -> None:
    resolved_ids, resolved_tp = resolve_vllm_gpu_config(gpu_ids, tensor_parallel_size)
    cmd = [
        "/opt/vllm-env/bin/vllm", "serve", model,
        "--port", str(port),
        "--api-key", api_key,
        "--max-model-len", str(max_model_len),
        "--tensor-parallel-size", str(resolved_tp),
        "--gpu-memory-utilization", str(gpu_memory_utilization),
        "--max_num_seqs", str(max_num_seqs),
    ]
    env = os.environ.copy()
    env["CUDA_VISIBLE_DEVICES"] = resolved_ids
    subprocess.Popen(cmd, env=env)
    logger.info(
        f"[OOM] vllm started on port {port} model={model} "
        f"GPUs={resolved_ids} TP={resolved_tp}"
    )


def _wait_for_vllm(port: int, timeout: int = 600) -> bool:
    url = f"http://localhost:{port}/health"
    for _ in range(timeout // 5):
        time.sleep(5)
        try:
            if requests.get(url, timeout=3).status_code == 200:
                logger.info(f"[OOM] vllm on port {port} is up ✓")
                return True
        except Exception:
            pass
    logger.error(f"[OOM] vllm on port {port} did not come up within {timeout}s")
    return False


def restart_vllm(cfg, on_failed_callback) -> None:
    """
    Kill vllm on cfg.vllm_port, restart it, wait for health.
    Calls on_failed_callback() if all retries exhausted.
    Intended to run in a thread (run_in_executor) — blocking.

    # TODO: Add logic to restart vllm if it is not responding
    """
    gc.collect()

    for attempt in range(1, MAX_OOM_RETRIES + 1):
        logger.warning(f"[OOM] restart attempt {attempt}/{MAX_OOM_RETRIES} port={cfg.vllm_port}")

        _kill_vllm_by_port(cfg.vllm_port)
        time.sleep(3)

        _start_vllm(
            port=cfg.vllm_port,
            model=cfg.vllm_model_name,
            api_key=cfg.vllm_api_key,
            max_model_len=cfg.max_model_len,
            tensor_parallel_size=cfg.tensor_parallel_size,
            gpu_memory_utilization=cfg.gpu_memory_utilization,
            max_num_seqs=cfg.max_num_seqs,
            gpu_ids=cfg.gpu_ids,
        )

        if _wait_for_vllm(cfg.vllm_port):
            return

    logger.error(f"[OOM] all retries exhausted for port {cfg.vllm_port} → calling failure callback")
    on_failed_callback()
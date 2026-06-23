
from __future__ import annotations

import os
import subprocess
import sys
import time
from concurrent.futures import ProcessPoolExecutor
from dataclasses import dataclass

MATRIX_SIZE = int(os.environ.get("BENCHMARK_MATRIX_SIZE", "4096"))
DURATION_SEC = float(os.environ.get("BENCHMARK_DURATION_SEC", "3.0"))
MIN_TFLOPS = float(os.environ.get("BENCHMARK_MIN_TFLOPS", "30.0"))

_AUTO_GPU_TOKENS = {"", "auto", "all"}

# Helper functions
def _default_gpu_count() -> int:
    """Return GPU count from env override, or auto-detect via torch.
    """
    if "BENCHMARK_GPU_COUNT" in os.environ:
        return int(os.environ["BENCHMARK_GPU_COUNT"])
    try:
        import torch
        n = torch.cuda.device_count()
        return n if n > 0 else 1
    except Exception:
        return 1

def _detect_gpu_ids_via_nvidia_smi() -> list[str] | None:
    """Return list of GPU indices visible to ``nvidia-smi``, or ``None`` on failure.
    """
    try:
        result = subprocess.run(
            ["nvidia-smi", "--query-gpu=index", "--format=csv,noheader"],
            capture_output=True, text=True, timeout=10,
        )
        if result.returncode != 0:
            return None
        indices = [ln.strip() for ln in result.stdout.splitlines() if ln.strip()]
        return indices or None
    except Exception:
        return None


def _detect_gpu_ids_via_torch() -> list[str] | None:
    try:
        import torch
        n = torch.cuda.device_count()
        return [str(i) for i in range(n)] if n > 0 else None
    except Exception:
        return None


def _detect_all_gpu_ids() -> list[str]:
    """Auto-detect visible GPUs (nvidia-smi first, torch fallback). Defaults to ``["0"]``."""
    return (
        _detect_gpu_ids_via_nvidia_smi()
        or _detect_gpu_ids_via_torch()
        or ["0"]
    )


def _largest_power_of_two_leq(n: int) -> int:
    """Return the largest power of two less than or equal to n."""
    p = 1
    while p * 2 <= n:
        p *= 2
    return p


def resolve_gpu_ids(gpu_ids: str | None) -> str:
    """Expand ``"all"`` / ``"auto"`` / empty / ``None`` to ``"0,1,...,N-1"``.
    """
    token = (gpu_ids or "").strip().lower()
    if token not in _AUTO_GPU_TOKENS:
        return gpu_ids  # type: ignore[return-value]
    return ",".join(_detect_all_gpu_ids())


# Resolve GPU configuration for vLLM ( only used for restarting vLLM if it is not responding)
def resolve_vllm_gpu_config(
    gpu_ids: str | None,
    tensor_parallel_size: int | None,
) -> tuple[str, int]:
    """Resolve ``(gpu_ids_csv, tensor_parallel_size)`` for a vLLM endpoint.
    """
    resolved_ids = resolve_gpu_ids(gpu_ids)
    n = len([x for x in resolved_ids.split(",") if x.strip()])

    if tensor_parallel_size is None or tensor_parallel_size <= 0:
        tp = max(1, n)
        if os.environ.get("VLLM_TP_POWER_OF_TWO", "").strip() in ("1", "true", "yes"):
            tp = _largest_power_of_two_leq(tp)
    else:
        tp = int(tensor_parallel_size)

    print(
        f"[gpu-resolve] GPU IDs: {resolved_ids!r} | Tensor Parallel Size: {tp} "
        f"(Input GPU IDs: {gpu_ids!r}, Input Tensor Parallel Size: {tensor_parallel_size!r})",
        file=sys.stderr,
        flush=True,
    )
    return resolved_ids, tp


# Benchmark 
@dataclass
class GPUBenchmarkResult:
    gpu_id: int
    gpu_name: str
    ops: int
    elapsed_sec: float
    tflops: float
    vram_gb: float
    passed: bool


def _benchmark_single_gpu(
    gpu_id: int,
    matrix_size: int,
    duration_sec: float,
    min_tflops: float,
) -> GPUBenchmarkResult:
    """Stress-test one GPU with repeated matrix multiplications"""
    import torch

    torch.cuda.set_device(gpu_id)
    device = torch.device(f"cuda:{gpu_id}")
    gpu_name = torch.cuda.get_device_name(gpu_id)

    props = torch.cuda.get_device_properties(gpu_id)
    vram_gb = round(props.total_memory / (1024**3), 1)

    a = torch.randn(matrix_size, matrix_size, device=device)
    b = torch.randn(matrix_size, matrix_size, device=device)

    for _ in range(3):
        torch.mm(a, b)
    torch.cuda.synchronize(device)

    ops = 0
    start = time.monotonic()
    while time.monotonic() - start < duration_sec:
        torch.mm(a, b)
        torch.cuda.synchronize(device)
        ops += 1
    elapsed = time.monotonic() - start

    flops_per_op = 2.0 * matrix_size**3
    tflops = round((ops * flops_per_op) / elapsed / 1e12, 2)

    return GPUBenchmarkResult(
        gpu_id=gpu_id,
        gpu_name=gpu_name,
        ops=ops,
        elapsed_sec=round(elapsed, 2),
        tflops=tflops,
        vram_gb=vram_gb,
        passed=tflops >= min_tflops,
    )


def run_benchmark(
    num_gpus: int | None = None,
    matrix_size: int = MATRIX_SIZE,
    duration_sec: float = DURATION_SEC,
    min_tflops: float = MIN_TFLOPS,
) -> list[GPUBenchmarkResult]:
    """Benchmark all available GPUs (or num_gpus if specified) in parallel"""
    import torch.multiprocessing as mp

    if num_gpus is None:
        num_gpus = _default_gpu_count()

    ctx = mp.get_context("spawn")
    with ProcessPoolExecutor(max_workers=num_gpus, mp_context=ctx) as pool:
        futures = [
            pool.submit(
                _benchmark_single_gpu,
                gpu_id,
                matrix_size,
                duration_sec,
                min_tflops,
            )
            for gpu_id in range(num_gpus)
        ]
        return [f.result() for f in futures]
"""Pre-flight hardware checks. Run BEFORE model downloads start.

Exits 0 if hardware is ok, exits 1 (touches /tmp/pod_replace) otherwise.
"""
from __future__ import annotations
import os
import sys
from pathlib import Path

REPLACE_FLAG = Path("/tmp/pod_replace")


def _benchmark_enabled() -> bool:
    """
    Read top-level `benchmark` from configuration.yaml.
    """
    try:
        import yaml
        path = os.environ.get("CONFIG_FILE", "/workspace/configuration.yaml")
        with open(path) as f:
            cfg = yaml.safe_load(f) or {}
        return bool(cfg.get("benchmark", True))
    except Exception as e:
        print(f"[preflight] could not read benchmark flag ({e}) — defaulting to True", file=sys.stderr)
        return True


def check_network() -> bool:
    from . import network as net_bench
    net = net_bench.run_benchmark()
    if net.crashed:
        print(
            f"[preflight] speedtest crashed (binary/network/parse error)",
            file=sys.stderr,
        )
        return True
    if not net.passed:
        print(
            f"[preflight] network degraded: "
            f"download={net.download_mbps} Mbps (min={net_bench.MIN_DOWNLOAD_MBPS}), "
            f"upload={net.upload_mbps} Mbps (min={net_bench.MIN_UPLOAD_MBPS}), "
            f"ping={net.ping_ms} ms",
            file=sys.stderr,
        )
        return False
    print(f"[preflight] network ok: {net.download_mbps} Mbps down, {net.upload_mbps} Mbps up")
    return True


def check_gpu() -> bool:
    from . import gpu as gpu_bench
    results = gpu_bench.run_benchmark()
    failed = [r for r in results if not r.passed]
    if failed:
        for r in failed:
            print(
                f"[preflight] GPU {r.gpu_id} ({r.gpu_name}) degraded: "
                f"tflops={r.tflops} (min={gpu_bench.MIN_TFLOPS}), "
                f"vram={r.vram_gb} GB",
                file=sys.stderr,
            )
        return False
    for r in results:
        print(f"[preflight] GPU {r.gpu_id} ({r.gpu_name}): {r.tflops} TFLOPS, {r.vram_gb} GB — ok")
    return True


def main() -> int:
    if not _benchmark_enabled():
        print("[preflight] benchmark disabled in config — skipping network + GPU checks")
        REPLACE_FLAG.unlink(missing_ok=True)
        return 0

    ok = True
    try:
        ok &= check_network()
    except Exception as e:
        print(f"[preflight] network check crashed: {e}", file=sys.stderr)
        ok = False

    try:
        ok &= check_gpu()
    except Exception as e:
        print(f"[preflight] gpu check crashed: {e}", file=sys.stderr)
        ok = False

    if not ok:
        REPLACE_FLAG.touch()
        return 1

    REPLACE_FLAG.unlink(missing_ok=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
from __future__ import annotations

import argparse
import base64
import json
import sys
import time
from pathlib import Path
from urllib.parse import urlparse

import httpx

GREEN = "\033[32m"
RED = "\033[31m"
YELLOW = "\033[33m"
CYAN = "\033[36m"
BOLD = "\033[1m"
RESET = "\033[0m"


def log_ok(msg: str) -> None:
    print(f"  {GREEN}OK{RESET}  {msg}")


def log_fail(msg: str) -> None:
    print(f"  {RED}FAIL{RESET}  {msg}")


def log_info(msg: str) -> None:
    print(f"  {CYAN}--{RESET}  {msg}")


def log_step(msg: str) -> None:
    print(f"\n{BOLD}[step]{RESET} {msg}")


def parse_prompts(path: str) -> list[dict]:
    prompts = []
    p = Path(path)
    if not p.exists():
        print(f"{RED}Prompts file not found: {path}{RESET}")
        sys.exit(1)
    for line in p.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        parts = line.split(None, 1)
        if len(parts) < 2:
            print(f"{YELLOW}Skipping malformed line: {line}{RESET}")
            continue
        prompts.append({"stem": parts[0], "image_url": parts[1]})
    return prompts


def wait_health(client: httpx.Client, max_wait: int = 30) -> bool:
    log_step("Health check")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        try:
            r = client.get("/health")
            if r.status_code == 200:
                log_ok(f"GET /health -> 200")
                return True
        except httpx.ConnectError:
            pass
        time.sleep(1)
    log_fail(f"Server not reachable after {max_wait}s")
    return False


def wait_ready(client: httpx.Client, max_wait: int = 3600) -> bool:
    log_step("Waiting for ready status")
    deadline = time.time() + max_wait
    while time.time() < deadline:
        r = client.get("/status")
        data = r.json()
        status = data["status"]
        if status == "warming_up":
            log_info(f"status={status}, waiting...")
            time.sleep(2)
            continue
        log_ok(f"GET /status -> {status}")
        return True
    log_fail(f"Still warming up after {max_wait}s")
    return False


def submit_batch(client: httpx.Client, prompts: list[dict], seed: int) -> bool:
    log_step(f"Submitting batch: {len(prompts)} prompts, seed={seed}")
    payload = {"prompts": prompts, "seed": seed}
    r = client.post("/generate", json=payload)
    if r.status_code == 200:
        data = r.json()
        log_ok(f"POST /generate -> accepted={data['accepted']}")
        return True
    log_fail(f"POST /generate -> {r.status_code}: {r.text}")
    return False


def poll_until_complete(client: httpx.Client, max_wait: int = 900) -> dict | None:
    log_step("Polling status")
    deadline = time.time() + max_wait
    last_progress = -1
    data: dict = {}
    while time.time() < deadline:
        r = client.get("/status")
        data = r.json()
        status = data["status"]
        progress = data["progress"]
        total = data["total"]

        if progress != last_progress:
            bar_width = 30
            filled = int(bar_width * progress / total) if total > 0 else 0
            bar = "█" * filled + "░" * (bar_width - filled)
            print(f"\r  [{bar}] {progress}/{total} {status}    ", end="", flush=True)
            last_progress = progress

        if status == "complete":
            print()
            log_ok(f"Complete: {progress}/{total}")
            return data

        time.sleep(1)

    print()
    log_fail(f"Timeout after {max_wait}s (status={data.get('status')}, {data.get('progress')}/{data.get('total')})")
    return None


def save_results(client: httpx.Client, results_dir: Path, stems: list[str]) -> list[dict]:
    log_step(f"Saving results to {results_dir}/")
    results_dir.mkdir(parents=True, exist_ok=True)
    records: list[dict] = []

    for stem in stems:
        r = client.get(f"/debug/tasks/{stem}")
        if r.status_code != 200:
            log_fail(f"{stem} -> HTTP {r.status_code}")
            records.append({"stem": stem, "error": f"HTTP {r.status_code}"})
            continue

        data = r.json()

        if data.get("js_code"):
            (results_dir / f"{stem}.js").write_text(data["js_code"], encoding="utf-8")

        if data.get("rendered_png_b64"):
            png_bytes = base64.b64decode(data["rendered_png_b64"])
            (results_dir / f"{stem}.png").write_bytes(png_bytes)

        if data.get("refinement_rendered_pngs_b64"):
            for i, png_b64 in enumerate(data["refinement_rendered_pngs_b64"]):
                png_bytes = base64.b64decode(png_b64)
                (results_dir / f"{stem}_refinement_{i+1}.png").write_bytes(png_bytes)
        if data.get("osd"):
            (results_dir / f"{stem}_osd.json").write_text(data["osd"], encoding="utf-8")

        input_saved = False
        image_url = data.get("image_url")
        if image_url:
            ext = Path(urlparse(image_url).path).suffix or ".png"
            try:
                img_resp = client.get(image_url, timeout=30.0, follow_redirects=True)
                if img_resp.status_code == 200:
                    (results_dir / f"{stem}_input{ext}").write_bytes(img_resp.content)
                    input_saved = True
                else:
                    log_fail(f"{stem} input image -> HTTP {img_resp.status_code}")
            except Exception as exc:
                log_fail(f"{stem} input image -> {type(exc).__name__}: {exc}")

        record = {k: v for k, v in data.items() if k not in ("js_code", "rendered_png_b64")}
        record["js_saved"] = bool(data.get("js_code"))
        record["png_saved"] = bool(data.get("rendered_png_b64"))
        record["osd_saved"] = bool(data.get("osd"))
        record["input_saved"] = input_saved
        records.append(record)

    (results_dir / "results.json").write_text(
        json.dumps(records, indent=2, ensure_ascii=False), encoding="utf-8"
    )
    log_ok(f"results.json written ({len(records)} records)")
    return records


def show_results(records: list[dict], results_dir: Path, elapsed: float) -> None:
    print(f"\n{BOLD}{'=' * 50}")
    print(f" Results")
    print(f"{'=' * 50}{RESET}\n")

    ok_count = 0
    failed_count = 0
    png_count = 0
    input_count = 0

    for rec in sorted(records, key=lambda r: r.get("stem", "")):
        stem = rec.get("stem", "?")

        if rec.get("input_saved"):
            input_count += 1

        if "error" in rec:
            log_fail(f"{stem} -> {rec['error']}")
            failed_count += 1
            continue

        if rec.get("failed"):
            reason = rec.get("failure_reason") or "unknown"
            log_fail(f"{stem}.js -> {reason}")
            failed_count += 1
            continue

        ok_count += 1
        parts = [f"{stem}.js"]

        js_path = results_dir / f"{stem}.js"
        if js_path.exists():
            size = js_path.stat().st_size
            lines = len(js_path.read_text(encoding="utf-8").splitlines())
            parts.append(f"{size:,} bytes, {lines} lines")

        if rec.get("png_saved"):
            png_count += 1
            png_path = results_dir / f"{stem}.png"
            if png_path.exists():
                parts.append(f"PNG {png_path.stat().st_size:,} bytes")
            else:
                parts.append("PNG saved")

        if rec.get("osd_saved"):
            parts.append("OSD saved")

        if rec.get("input_saved"):
            parts.append("input saved")

        log_ok(" | ".join(parts))

    total = ok_count + failed_count
    print(f"\n{BOLD}{'=' * 50}")
    print(f" Summary")
    print(f"{'=' * 50}{RESET}\n")
    color = GREEN if ok_count == total else YELLOW if ok_count > 0 else RED
    print(f"  {color}{ok_count}/{total} passed{RESET}, {failed_count} failed, {png_count} with PNG, {input_count} with input")
    print(f"  Pipeline time: {elapsed:.1f}s")
    print(f"  Output dir: {results_dir.resolve()}")
    print()


def main() -> None:
    parser = argparse.ArgumentParser(description="Pipeline smoke test")
    parser.add_argument("prompts_file", nargs="?", default="tests/prompts/test_prompts.txt")
    parser.add_argument("--host", default="localhost")
    parser.add_argument("--port", type=int, default=10006)
    parser.add_argument("--seed", type=int, default=42)
    parser.add_argument("--timeout", type=int, default=9000, help="max poll time in seconds")
    parser.add_argument("--limit", type=int, default=None, help="limit the number of prompts to test")
    parser.add_argument(
        "--name",
        default="test",
        help="output folder name (under the prompts file directory); files are written there",
    )
    args = parser.parse_args()

    prompts = parse_prompts(args.prompts_file)
    if not prompts:
        print(f"{RED}No prompts found in {args.prompts_file}{RESET}")
        sys.exit(1)

    if args.limit:
        prompts = prompts[:args.limit]

    stems = [p["stem"] for p in prompts]
    results_dir = Path(args.prompts_file).parent / args.name

    base_url = f"http://{args.host}:{args.port}"
    print(f"\n{BOLD}Pipeline Test{RESET}")
    print(f"  Target: {base_url}")
    print(f"  Prompts: {len(prompts)} from {args.prompts_file}")
    print(f"  Output: {results_dir}/ (--name={args.name})")
    print(f"  Seed: {args.seed}")

    client = httpx.Client(base_url=base_url, timeout=30.0)

    try:
        if not wait_health(client):
            sys.exit(1)

        if not wait_ready(client):
            sys.exit(1)

        t_start = time.time()

        if not submit_batch(client, prompts, args.seed):
            sys.exit(1)

        if poll_until_complete(client, max_wait=args.timeout) is None:
            sys.exit(1)

        elapsed = time.time() - t_start

        records = save_results(client, results_dir, stems)
        show_results(records, results_dir, elapsed)

    finally:
        client.close()


if __name__ == "__main__":
    main()

"""
tools/check_container.py  -  Part 3, Step 3: check the Docker image and a container run from it, under the
same limits as the Fargate task. GitHub Actions runs the same script in Step 6.

Usage (from the candidate-rag folder, venv active, Docker Desktop running, image already built):
    docker build -t candidate-rag-mcp:local .
    python tools/check_container.py                     # 1 GB, 0.25 vCPU: the Fargate task size
    python tools/check_container.py --memory 512m       # the smaller size, for comparison only

What it checks:
  * the image: runs as a non-root user, has no real-data switch set, contains only the allowlisted files,
    has no data/, jds/, chroma/ or .env, and has no Anthropic SDK installed;
  * a container started with a read-only filesystem (only /tmp writable) and the memory and CPU limits:
    it becomes healthy, passes check_http.py and (if Node.js is present) the 12 Inspector checks over HTTP,
    writes nothing outside /tmp, and its memory after all that is recorded;
  * `docker stop` (SIGTERM, as ECS sends): it exits with code 0 within 30 s and logs "stopped".
Prints PASS or FAIL per check. Sample data only; no API calls, no cost.
"""
import argparse
import json
import shutil
import socket
import subprocess
import sys
import threading
import time
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
NAME = "candidate-rag-check"
EXPECTED_FILES = {  # everything under /app that the allowlist should let in, plus what the build makes
    "requirements-server.txt", "src/mcp_server.py", "src/search.py", "src/paths.py", "src/index.py",
    "src/retrieve.py", "sample/profiles.jsonl", "sample/jds/credit-controller.txt", "sample/jds/semi-senior.txt",
}
MEMORY_CEILING_MB = 700  # the size decision is revisited above this (build guide, Step 3)
results = []

INSIDE = r"""
import importlib.util, json, os
files, chroma = [], 0
for root, dirs, names in os.walk("/app"):
    for n in names:
        rel = os.path.relpath(os.path.join(root, n), "/app").replace(os.sep, "/")
        if rel.startswith("sample/chroma/"):
            chroma += 1
        else:
            files.append(rel)
print(json.dumps({
    "files": sorted(files), "sample_index_files": chroma,
    "forbidden": [p for p in ("data", "jds", "chroma", ".env", ".git", ".venv") if os.path.exists("/app/" + p)],
    "anthropic": importlib.util.find_spec("anthropic") is not None,
    "uid": os.getuid(),
    "real_switch": os.environ.get("CANDIDATE_RAG_REAL"),
}))
"""


def record(name: str, ok: bool, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail[:240]}")


def docker(*args: str, check: bool = False, timeout: int = 120) -> subprocess.CompletedProcess:
    return subprocess.run(["docker", *args], capture_output=True, text=True, encoding="utf-8", errors="replace",
                          check=check, timeout=timeout)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def last_line(text: str) -> str:
    lines = [ln for ln in text.strip().splitlines() if ln.strip()]
    return lines[-1] if lines else ""


def mem_mb(usage: str) -> float | None:
    """'312.4MiB / 1GiB' -> 312.4"""
    try:
        used = usage.split("/")[0].strip()
        num = float("".join(c for c in used if c.isdigit() or c == "."))
        return num * 1024 if "GiB" in used else num / 1024 if "KiB" in used else num
    except ValueError:
        return None


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--image", default="candidate-rag-mcp:local")
    ap.add_argument("--memory", default="1g", help="container memory limit (Fargate task: 1g)")
    ap.add_argument("--cpus", default="0.25", help="container CPU limit (Fargate task: 0.25)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not shutil.which("docker"):
        print("FAIL  docker not found. Start Docker Desktop and open a new PowerShell window.")
        return 1

    # ---------------------------------------------------------------- the image itself
    r = docker("image", "inspect", args.image)
    if r.returncode != 0:
        print(f"FAIL  image {args.image} not found. Build it first: docker build -t {args.image} .")
        return 1
    info = json.loads(r.stdout)[0]
    cfg = info.get("Config", {})
    size_mb = info.get("Size", 0) / 1e6
    print(f"image {args.image}: {size_mb:.0f} MB, created {info.get('Created', '')[:19]}")
    record("image runs as a non-root user", cfg.get("User") not in (None, "", "0", "root"), f"User = {cfg.get('User')!r}")
    record("image never sets the real-data switch", not any(e.startswith("CANDIDATE_RAG_REAL") for e in cfg.get("Env", [])),
           ", ".join(e for e in cfg.get("Env", []) if e.startswith(("MCP_", "CANDIDATE", "ANONYMIZED", "HOME"))))

    r = docker("run", "--rm", "--network", "none", "--entrypoint", "python", args.image, "-c", INSIDE)
    try:
        inside = json.loads(last_line(r.stdout))
    except json.JSONDecodeError:
        record("image contents could be read", False, (r.stdout + r.stderr)[-240:])
        return finish()
    files = set(inside["files"])
    record("only the allowlisted files are in /app, plus the sample index built inside",
           files == EXPECTED_FILES and inside["sample_index_files"] > 0,
           f"{len(files)} files, sample index {inside['sample_index_files']} files; "
           f"unexpected: {sorted(files - EXPECTED_FILES) or 'none'}; missing: {sorted(EXPECTED_FILES - files) or 'none'}")
    record("no data/, jds/, chroma/, .env, .git or .venv in the image", not inside["forbidden"],
           f"found: {inside['forbidden'] or 'none'}")
    record("the Anthropic SDK is not installed in the image", not inside["anthropic"],
           "import anthropic would fail" if not inside["anthropic"] else "anthropic is installed")

    # ---------------------------------------------------------------- a container under the Fargate limits
    docker("rm", "-f", NAME)
    port = free_port()
    r = docker("run", "-d", "--name", NAME, "-p", f"127.0.0.1:{port}:8000", "--read-only", "--tmpfs", "/tmp",
               f"--memory={args.memory}", f"--cpus={args.cpus}", args.image)
    if r.returncode != 0:
        record("container starts", False, r.stderr[-240:])
        return finish()
    print(f"container {NAME} on 127.0.0.1:{port}, read-only, --memory={args.memory} --cpus={args.cpus}")
    base = f"http://127.0.0.1:{port}"

    try:
        container_checks(base, args.memory)
    finally:
        stop_and_check()
    return finish()


def container_checks(base: str, memory: str) -> None:
    t0, ready = time.monotonic(), False
    while time.monotonic() - t0 < 240 and not ready:
        state = json.loads(docker("inspect", NAME).stdout or "[{}]")[0].get("State", {})
        if not state.get("Running"):
            break
        ready = subprocess.run([sys.executable, "-c", f"import httpx,sys; sys.exit(0 if httpx.get('{base}/health', timeout=2).status_code == 200 else 1)"],
                               capture_output=True).returncode == 0
        if not ready:
            time.sleep(1)
    out = docker("logs", NAME)
    logs = out.stdout + out.stderr
    record("container becomes healthy with a read-only filesystem and the limits", ready,
           f"healthy after {time.monotonic() - t0:.1f} s" if ready
           else f"not healthy; OOM killed: {state.get('OOMKilled')}; exit code {state.get('ExitCode')}; "
                f"last log line: {last_line(logs)}")
    if not ready:
        return

    # Sample memory every half second for the whole run, so the peak is caught, not just the level afterwards
    # (on 20/10 ten parallel searches briefly took the container past 1 GB, then memory fell back).
    samples: list[float] = []
    stop = threading.Event()

    def sample() -> None:
        while not stop.is_set():
            used = mem_mb(docker("stats", NAME, "--no-stream", "--format", "{{.MemUsage}}").stdout.strip())
            if used is not None:
                samples.append(used)
            stop.wait(0.5)
    sampler = threading.Thread(target=sample, daemon=True)
    sampler.start()

    r = subprocess.run([sys.executable, str(PROJECT / "tools" / "check_http.py"), "--url", base],
                       capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=600)
    record("check_http.py passes against the container", r.returncode == 0, last_line(r.stdout))
    if r.returncode != 0:
        show_failure(r)

    if shutil.which("npx"):
        r = subprocess.run([sys.executable, str(PROJECT / "tools" / "inspector_check.py"), "--http", f"{base}/mcp"],
                           capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=900)
        record("the 12 Inspector checks pass against the container", r.returncode == 0, last_line(r.stdout))
        if r.returncode != 0:
            show_failure(r)
    else:
        print("SKIP  Inspector checks: Node.js not found")

    stop.set()
    sampler.join(timeout=10)
    peak = max(samples) if samples else None
    state = json.loads(docker("inspect", NAME).stdout or "[{}]")[0].get("State", {})
    record(f"peak memory during all the checks is under {MEMORY_CEILING_MB} MB, never OOM-killed",
           peak is not None and peak < MEMORY_CEILING_MB and not state.get("OOMKilled") and state.get("Running"),
           f"peak {peak:.0f} MiB over {len(samples)} samples (limit {memory}); still running: {state.get('Running')}; "
           f"OOM killed: {state.get('OOMKilled')}" if peak is not None else "no memory samples")

    diff = docker("diff", NAME).stdout.strip()
    record("nothing written outside /tmp (docker diff is empty)", diff == "", diff.replace("\n", ", ") or "no changes")


def show_failure(r: subprocess.CompletedProcess) -> None:
    """A failed sub-check: its FAIL lines, and the end of its error output if it crashed."""
    lines = r.stdout.splitlines()
    for i, ln in enumerate(lines):
        if ln.startswith("FAIL"):
            print(f"      {ln}")
            if i + 1 < len(lines) and lines[i + 1].startswith("      "):
                print(f"      {lines[i + 1]}")  # the failing check's detail line: what actually happened
    err = [ln for ln in r.stderr.strip().splitlines() if ln.strip()]
    if err:
        print("      error output (last lines):")
        for ln in err[-4:]:
            print(f"        {ln[:200]}")


def stop_and_check() -> None:
    t0 = time.monotonic()
    docker("stop", "-t", "30", NAME, timeout=60)
    took = time.monotonic() - t0
    state = json.loads(docker("inspect", NAME).stdout or "[{}]")[0].get("State", {})
    logs = docker("logs", NAME)
    text = logs.stdout + logs.stderr
    record("docker stop (SIGTERM) ends it within 30 s with exit code 0, logging 'stopped'",
           state.get("ExitCode") == 0 and took < 30 and "stopped" in text,
           f"exit code {state.get('ExitCode')} after {took:.1f} s; 'stopped' logged: {'stopped' in text}; "
           f"OOM killed: {state.get('OOMKilled')}")
    docker("rm", "-f", NAME)


def finish() -> int:
    print("=" * 70)
    print(f"{sum(results)} of {len(results)} container checks passed")
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

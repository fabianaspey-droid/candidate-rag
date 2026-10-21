"""
tools/check_http.py  -  Part 3 check of the Streamable HTTP transport against a real server process.

Two ways to run it (from the candidate-rag folder, venv active):
    python tools/check_http.py                      # starts its own server (sample, --http, a free local
                                                    # port), checks it, then stops it with a signal
    python tools/check_http.py --url http://127.0.0.1:8000
                                                    # checks a server that is already running (the container
                                                    # in Step 3, the AWS service in Step 4); give the base URL,
                                                    # without /mcp

Prints PASS or FAIL per check, so the result can be pasted. Sample data only. No API calls, no cost.
What only HTTP has: the health route, the Host and Origin checks, stateless calls with no session id, start-up
only after warm-up, and a clean stop on a signal. The tools' own guards are covered by check_guards.py and
inspector_check.py.
"""
import argparse
import concurrent.futures
import os
import signal
import socket
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import httpx

PROJECT = Path(__file__).resolve().parents[1]
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
           "MCP-Protocol-Version": "2025-06-18"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "check_http", "version": "0"}}}
START_LIMIT_S = 240   # the load balancer's grace period is 300 s; start-up must fit well inside it
STOP_LIMIT_S = 30     # ECS sends SIGKILL 30 s after SIGTERM
results = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail[:220]}")


def temp_copies() -> set[str]:
    return {p.name for p in Path(tempfile.gettempdir()).glob("candidate_rag_index_*")}


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def rpc(client: httpx.Client, base: str, method: str, params: dict | None = None, rid: int = 2, **headers):
    body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
    return client.post(f"{base}/mcp", json=body, headers={**HEADERS, **headers})


def call_tool(client, base, name, arguments):
    r = rpc(client, base, "tools/call", {"name": name, "arguments": arguments})
    return r, (r.json().get("result", {}) if r.headers.get("content-type", "").startswith("application/json") else {})


def start_server(port: int, log_path: Path) -> subprocess.Popen:
    env = dict(os.environ, MCP_PORT=str(port), MCP_HOST="127.0.0.1")
    env.pop("CANDIDATE_RAG_REAL", None)  # always the sample
    flags = subprocess.CREATE_NEW_PROCESS_GROUP if os.name == "nt" else 0  # lets us send Ctrl+Break on Windows
    log = log_path.open("w", encoding="utf-8")
    return subprocess.Popen([sys.executable, str(PROJECT / "src" / "mcp_server.py"), "--http"], cwd=PROJECT,
                            env=env, stdout=log, stderr=subprocess.STDOUT, creationflags=flags)


def wait_until_listening(base: str, proc: subprocess.Popen | None) -> tuple[float | None, int]:
    """Seconds until /health answered, and how many earlier attempts found nothing listening."""
    t0, refused = time.monotonic(), 0
    while time.monotonic() - t0 < START_LIMIT_S:
        if proc is not None and proc.poll() is not None:
            return None, refused
        try:
            if httpx.get(f"{base}/health", timeout=2).status_code == 200:
                return time.monotonic() - t0, refused
        except httpx.TransportError:
            refused += 1
        time.sleep(0.5)
    return None, refused


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--url", help="base URL of a running server, e.g. http://127.0.0.1:8000 (default: start one)")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    own = args.url is None
    proc, log_path, before = None, Path(tempfile.gettempdir()) / "candidate_rag_check_http.log", temp_copies()
    if own:
        base = f"http://127.0.0.1:{free_port()}"
        proc = start_server(int(base.rsplit(":", 1)[1]), log_path)
    else:
        base = args.url.rstrip("/").removesuffix("/mcp")

    took, refused = wait_until_listening(base, proc)
    if own:
        log_text = log_path.read_text(encoding="utf-8", errors="replace")
        warm_at, listen_at = log_text.find("warm:"), log_text.find("Uvicorn running")
        record("server listens only after warm-up, within the start-up limit",
               took is not None and 0 <= warm_at < listen_at,
               f"ready after {took:.1f} s; {refused} earlier attempts found nothing listening; "
               f"'warm:' logged before 'Uvicorn running': {0 <= warm_at < listen_at}" if took is not None
               else "never became ready; last log lines: " + " | ".join(log_text.strip().splitlines()[-3:]))
    else:
        record("/health answers", took is not None, f"answered after {took:.1f} s" if took is not None else "no answer")
    if took is None:
        return finish(proc, before, log_path, own)

    with httpx.Client(timeout=30) as c:
        r = c.get(f"{base}/health")
        record("/health says ok, sample mode", r.status_code == 200 and r.json() == {"status": "ok", "mode": "sample"},
               f"{r.status_code} {r.text[:100]}")

        r = c.post(f"{base}/mcp", json=INIT, headers=HEADERS)
        ok = r.status_code == 200 and r.headers.get("content-type", "").startswith("application/json")
        record("initialize with the right host: plain JSON reply from candidate-rag",
               ok and r.json()["result"]["serverInfo"]["name"] == "candidate-rag",
               f"{r.status_code}, content-type {r.headers.get('content-type')}")

        r = c.post(f"{base}/mcp", json=INIT, headers={**HEADERS, "Host": "evil.example.net"})
        expected = {421} if own else {400, 403, 404, 421}  # through a load balancer, its host rule may refuse first
        record("a request naming another host is refused", r.status_code in expected, f"{r.status_code} {r.text[:80]}")

        bad = c.post(f"{base}/mcp", json=INIT, headers={**HEADERS, "Origin": "https://evil.example.net"}).status_code
        good = c.post(f"{base}/mcp", json=INIT, headers={**HEADERS, "Origin": "https://claude.ai"}).status_code
        record("a foreign browser origin gets 403; claude.ai is allowed", bad == 403 and good == 200,
               f"foreign {bad}, claude.ai {good}")

        r, res = call_tool(c, base, "list_roles", {})
        record("tool call with no session id works (stateless); sample mode",
               r.status_code == 200 and not res.get("isError") and res.get("structuredContent", {}).get("mode") == "sample"
               and "mcp-session-id" not in {k.lower() for k in r.headers},
               f"{r.status_code}, mode {res.get('structuredContent', {}).get('mode')}, "
               f"session header {'present' if 'mcp-session-id' in {k.lower() for k in r.headers} else 'absent'}")

        t0 = time.monotonic()
        r, res = call_tool(c, base, "search_candidates", {"role": "semi-senior", "query": "AAT qualified semi senior accountant", "n_results": 5})
        one_search = time.monotonic() - t0
        hits = res.get("structuredContent", {}).get("results", [])
        record("search over HTTP returns 5 ranked results", not res.get("isError") and [h["rank"] for h in hits] == [1, 2, 3, 4, 5],
               f"in {one_search:.1f} s: " + (", ".join(f"{h['rank']}. {h['id']} {h['title']}" for h in hits) or str(res)[:200]))

        r, res = call_tool(c, base, "get_profile", {"candidate_id": 10})
        record("get_profile over HTTP (numeric id)", not res.get("isError")
               and res.get("structuredContent", {}).get("profile", "").startswith("Current role:"),
               res.get("structuredContent", {}).get("profile", "")[:100] or str(res)[:200])

        r, res = call_tool(c, base, "search_candidates", {"role": "semi-senior", "query": "x", "n_results": 21})
        record("guards still apply over HTTP (21 results refused)", bool(res.get("isError")),
               " ".join(x.get("text", "") for x in res.get("content", []))[:150])

        def one(i):
            t = time.monotonic()
            try:
                with httpx.Client(timeout=60) as cc:
                    _, rr = call_tool(cc, base, "search_candidates", {"role": "credit-controller", "query": f"credit control {i}", "n_results": 3})
                    ok = not rr.get("isError") and len(rr.get("structuredContent", {}).get("results", [])) == 3
                    return ("ok" if ok else "wrong reply"), time.monotonic() - t
            except httpx.HTTPError as e:  # on 20/10 an out-of-memory crash showed up here as a dropped connection
                return type(e).__name__, time.monotonic() - t
        t0 = time.monotonic()
        with concurrent.futures.ThreadPoolExecutor(10) as ex:
            outcomes = list(ex.map(one, range(10)))
        errors = sorted({o for o, _ in outcomes if o != "ok"})
        waits = sorted(w for _, w in outcomes)
        record("10 searches at once all succeed", not errors,
               f"{sum(o == 'ok' for o, _ in outcomes)}/10 in {time.monotonic() - t0:.1f} s; each waited {waits[0]:.1f} to {waits[-1]:.1f} s"
               + (f"; errors: {', '.join(errors)}" if errors else ""))
    return finish(proc, before, log_path, own)


def finish(proc, before, log_path, own) -> int:
    if own and proc is not None:
        if proc.poll() is None:
            proc.send_signal(signal.CTRL_BREAK_EVENT if os.name == "nt" else signal.SIGTERM)
            t0 = time.monotonic()
            try:
                code = proc.wait(timeout=STOP_LIMIT_S)
                took = time.monotonic() - t0
            except subprocess.TimeoutExpired:
                proc.kill()
                code, took = None, None
            time.sleep(0.5)
            text = log_path.read_text(encoding="utf-8", errors="replace")
            left = temp_copies() - before
            record("a stop signal ends it cleanly within 30 s, temporary index copy removed",
                   code == 0 and "stopped" in text and not left,
                   f"exit code {code} after {took:.1f} s; 'stopped' logged: {'stopped' in text}; "
                   f"copies left: {sorted(left) or 'none'}" if took is not None else "did not stop within 30 s (killed)")
        else:
            record("server still running at the end", False, f"exited early with code {proc.returncode}; see {log_path}")
    print("=" * 70)
    print(f"{sum(results)} of {len(results)} HTTP checks passed" + (f"  (server log: {log_path})" if own else ""))
    return 0 if results and all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

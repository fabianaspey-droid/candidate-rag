"""
tools/inspector_check.py  -  Step 5 check with the official MCP Inspector (command-line mode).

Runs `npx @modelcontextprotocol/inspector@2.9.0 --cli <this venv's python> src/mcp_server.py ...` for each
check and prints PASS or FAIL, so the result can be pasted. The Inspector launches the server itself over
stdio, as any MCP client would. Sample data only. No API calls, no cost.

Inspector 2.9.0 is pinned (released 30/09/2026; 2.11.0 came out on 14/10, too new to trust). It needs
Node.js 22.19 or later. It keeps a small catalog file in your user folder (.mcp-inspector), which does not
touch this project. Secrets are kept in memory only (MCP_INSPECTOR_SECRET_STORE=memory).

Usage (from the candidate-rag folder, venv active):
    python tools/inspector_check.py                                   # stdio: the Inspector launches the server
    python tools/inspector_check.py --http http://127.0.0.1:8000/mcp  # Streamable HTTP: a server already running
                                                                      # (Part 3), the same 12 checks
"""
import argparse
import json
import os
import shutil
import subprocess
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
INSPECTOR = "@modelcontextprotocol/inspector@2.9.0"
HTTP_URL = None  # set by --http; None means stdio
results = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail[:200]}")


def inspector(*args: str) -> tuple[dict | None, str, int]:
    npx = shutil.which("npx")
    if HTTP_URL:
        cmd = [npx, "-y", INSPECTOR, "--cli", "--transport", "http", "--server-url", HTTP_URL, *args]
    else:
        cmd = [npx, "-y", INSPECTOR, "--cli", sys.executable, str(PROJECT / "src" / "mcp_server.py"), *args]
    env = dict(os.environ, MCP_INSPECTOR_SECRET_STORE="memory")
    p = subprocess.run(cmd, cwd=PROJECT, capture_output=True, text=True, encoding="utf-8", errors="replace",
                       env=env, timeout=180)
    try:
        return json.loads(p.stdout), p.stderr, p.returncode
    except json.JSONDecodeError:
        return None, (p.stdout + p.stderr)[-300:], p.returncode


def main() -> int:
    global HTTP_URL
    ap = argparse.ArgumentParser()
    ap.add_argument("--http", metavar="URL", help="check a running server over Streamable HTTP, e.g. http://127.0.0.1:8000/mcp")
    HTTP_URL = ap.parse_args().http
    print(f"transport: {'Streamable HTTP, ' + HTTP_URL if HTTP_URL else 'stdio (the Inspector launches the server)'}")
    if not shutil.which("npx") or not shutil.which("node"):
        print("FAIL  Node.js not found. Install Node.js 22.19 or later, then open a new PowerShell window.")
        return 1
    version = subprocess.run([shutil.which("node"), "--version"], capture_output=True, text=True).stdout.strip()
    major, minor = (int(x) for x in version.lstrip("v").split(".")[:2])
    record(f"Node.js {version} is at least 22.19", (major, minor) >= (22, 19), version)
    if (major, minor) < (22, 19):
        return 1

    d, err, code = inspector("--method", "tools/list", "--strict")
    tools = (d or {}).get("tools", [])
    record("Inspector lists exactly the agreed tools, all read-only",
           sorted(t["name"] for t in tools) == ["get_profile", "list_roles", "search_candidates"]
           and all(t.get("annotations", {}).get("readOnlyHint") for t in tools),
           ", ".join(f"{t['name']} (readOnlyHint {t.get('annotations', {}).get('readOnlyHint')})" for t in tools) or err)
    record("--strict: no error-severity schema problems", code == 0,
           f"exit code {code}; " + " ".join(line for line in err.splitlines() if "issue" in line.lower() or "portab" in line.lower())[:150])

    d, err, _ = inspector("--method", "resources/templates/list")
    tpl = [t["uriTemplate"] for t in (d or {}).get("resourceTemplates", [])]
    record("Inspector lists the jd://{role} resource", tpl == ["jd://{role}"], str(tpl or err))

    d, err, _ = inspector("--method", "prompts/list")
    pr = [(p["name"], [a["name"] for a in p.get("arguments", [])]) for p in (d or {}).get("prompts", [])]
    record("Inspector lists the shortlist_for_role prompt", pr == [("shortlist_for_role", ["role", "how_many"])], str(pr or err))

    d, err, _ = inspector("--method", "tools/call", "--tool-name", "list_roles")
    sc = (d or {}).get("structuredContent", {})
    record("list_roles runs, in sample mode", not (d or {}).get("isError") and sc.get("mode") == "sample",
           f"mode {sc.get('mode')}, roles {[r['role'] for r in sc.get('roles', [])]}" if d else err)

    d, err, _ = inspector("--method", "tools/call", "--tool-name", "search_candidates",
                          "--tool-arg", "role=semi-senior", "query=AAT qualified semi senior accountant", "n_results=5")
    sc = (d or {}).get("structuredContent", {})
    record("search_candidates returns 5 ranked results", not (d or {}).get("isError") and len(sc.get("results", [])) == 5,
           ", ".join(f"{h['rank']}. {h['id']} {h['title']}" for h in sc.get("results", [])) or (json.dumps(d)[:200] if d else err))

    d, err, _ = inspector("--method", "tools/call", "--tool-name", "search_candidates",
                          "--tool-arg", "role=semi-senior", "query=accountant", "n_results=21")
    record("21 results is rejected", bool((d or {}).get("isError")), (d or {}).get("content", [{}])[0].get("text", err)[:150])

    d, err, _ = inspector("--method", "tools/call", "--tool-name", "get_profile", "--tool-arg", "candidate_id=10")
    sc = (d or {}).get("structuredContent", {})
    record("get_profile returns a profile (the Inspector sends the id as a number)",
           not (d or {}).get("isError") and sc.get("profile", "").startswith("Current role:"),
           sc.get("profile", "")[:120] or (json.dumps(d)[:200] if d else err))

    d, err, _ = inspector("--method", "tools/call", "--tool-name", "get_profile", "--tool-arg", "candidate_id=99999")
    record("made-up id is rejected", bool((d or {}).get("isError")), (d or {}).get("content", [{}])[0].get("text", err)[:150])

    d, err, _ = inspector("--method", "resources/read", "--uri", "jd://credit-controller")
    first = ((d or {}).get("contents") or [{}])[0].get("text", "").splitlines()[:1]
    record("jd://credit-controller returns the advert", bool(first), first[0] if first else err)

    d, err, _ = inspector("--method", "prompts/get", "--prompt-name", "shortlist_for_role",
                          "--prompt-args", "role=semi-senior", "how_many=3")
    msg = ((d or {}).get("messages") or [{}])[0].get("content", {}).get("text", "")
    record("shortlist_for_role prompt renders", "jd://semi-senior" in msg, msg.splitlines()[0] if msg else err)

    print("=" * 70)
    print(f"{sum(results)} of {len(results)} Inspector checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

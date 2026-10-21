"""
Tests for the Streamable HTTP transport (Part 3, Step 2), in-process through Starlette's test client: the
health route, the Host and Origin checks, stateless calls with no session id, and the same tools as stdio.

These tests make no search, so they need no index and no embedding model. Search over HTTP, start-up,
warm-up and shutdown are checked against a real server process by tools/check_http.py.
"""
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from starlette.testclient import TestClient  # noqa: E402

import mcp_server  # noqa: E402

PUBLIC_HOST = "demo.example.test"
HEADERS = {"Content-Type": "application/json", "Accept": "application/json, text/event-stream",
           "MCP-Protocol-Version": "2025-06-18"}
INIT = {"jsonrpc": "2.0", "id": 1, "method": "initialize",
        "params": {"protocolVersion": "2025-06-18", "capabilities": {}, "clientInfo": {"name": "pytest", "version": "0"}}}


def client():
    return TestClient(mcp_server.http_app(allowed_hosts=PUBLIC_HOST, allowed_origins=""),
                      base_url=f"http://{PUBLIC_HOST}")


def rpc(c, method, params=None, rid=2, **headers):
    body = {"jsonrpc": "2.0", "id": rid, "method": method, "params": params or {}}
    return c.post("/mcp", json=body, headers={**HEADERS, **headers})


def test_health_answers_whatever_the_host_header_says():
    with client() as c:
        r = c.get("/health", headers={"Host": "10.0.3.7:8000"})  # a load balancer checks by the task's IP
        assert r.status_code == 200 and r.json() == {"status": "ok", "mode": "sample"}


def test_allowed_host_gets_a_normal_initialize_reply():
    with client() as c:
        r = c.post("/mcp", json=INIT, headers=HEADERS)
        assert r.status_code == 200 and r.headers["content-type"].startswith("application/json")
        assert r.json()["result"]["serverInfo"]["name"] == "candidate-rag"


def test_wrong_host_is_refused_with_421():
    with client() as c:
        r = c.post("/mcp", json=INIT, headers={**HEADERS, "Host": "evil.example.net"})
        assert r.status_code == 421


def test_foreign_origin_is_refused_and_claude_origin_is_not():
    with client() as c:
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Origin": "https://evil.example.net"}).status_code == 403
        assert c.post("/mcp", json=INIT, headers={**HEADERS, "Origin": "https://claude.ai"}).status_code == 200


def test_stateless_tool_call_needs_no_session():
    with client() as c:
        r = rpc(c, "tools/call", {"name": "list_roles", "arguments": {}})
        body = r.json()
        assert r.status_code == 200 and "mcp-session-id" not in {k.lower() for k in r.headers}
        assert body["result"]["isError"] is False and body["result"]["structuredContent"]["mode"] == "sample"


def test_same_tools_resource_and_prompt_as_stdio():
    with client() as c:
        tools = sorted(t["name"] for t in rpc(c, "tools/list").json()["result"]["tools"])
        templates = [t["uriTemplate"] for t in rpc(c, "resources/templates/list").json()["result"]["resourceTemplates"]]
        prompts = [p["name"] for p in rpc(c, "prompts/list").json()["result"]["prompts"]]
    assert tools == ["get_profile", "list_roles", "search_candidates"]
    assert templates == ["jd://{role}"] and prompts == ["shortlist_for_role"]


def test_guards_still_apply_over_http():
    with client() as c:
        r = rpc(c, "tools/call", {"name": "search_candidates", "arguments": {"role": "semi-senior", "query": "x", "n_results": 21}})
        assert r.json()["result"]["isError"] is True


def test_host_patterns_always_allow_local_and_add_public_hosts():
    hosts = mcp_server._host_patterns(" Svc.ECS.eu-west-2.on.aws , ")
    assert hosts[:3] == ["127.0.0.1:*", "localhost:*", "[::1]:*"]
    assert "svc.ecs.eu-west-2.on.aws" in hosts and "svc.ecs.eu-west-2.on.aws:*" in hosts
    assert mcp_server._host_patterns("") == ["127.0.0.1:*", "localhost:*", "[::1]:*"]


def test_refused_calls_never_put_the_callers_text_in_the_log(caplog):
    # Part 3, Step 5 (21/10): the SDK logged our refusal messages, which repeat what the caller sent.
    marker = "canary0123456789"
    with caplog.at_level("INFO"), client() as c:
        r1 = rpc(c, "tools/call", {"name": "search_candidates", "arguments": {"role": marker, "query": "credit control"}})
        r2 = rpc(c, "tools/call", {"name": "get_profile", "arguments": {"candidate_id": marker}})
        r3 = rpc(c, "tools/call", {"name": "search_candidates", "arguments": {"role": "semi-senior", "query": marker + " x" * 300}})
        r4 = rpc(c, "resources/read", {"uri": f"jd://{marker}"})
    assert [r.status_code for r in (r1, r2, r3, r4)] == [200, 200, 200, 200]
    assert all(r.json()["result"]["isError"] for r in (r1, r2, r3))  # still refused, with the message, to the caller
    assert marker in r1.json()["result"]["content"][0]["text"]
    assert "error" in r4.json()
    assert "refused the call" in caplog.text and "Resource read refused" in caplog.text  # still visible as events
    assert marker not in caplog.text

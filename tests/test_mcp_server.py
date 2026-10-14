"""
Tests for the MCP server THROUGH the MCP layer: every call goes through the SDK's Client, connected to
src/mcp_server.py in-process, exactly as an assistant would see it. No API calls, no model download.

The index is a throwaway copy of the invented sample, embedded with a tiny deterministic stand-in for the
real model, so these tests check the MCP layer and its guards, not search quality (Part 1 measured that).
"""
import asyncio
import hashlib
import json
import re
import shutil
import sys
from pathlib import Path

import numpy as np
import pytest

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT / "src"))

from chromadb.utils.embedding_functions import onnx_mini_lm_l6_v2 as onnx  # noqa: E402


def _stand_in_embedder(self, input):
    out = []
    for t in input:
        v = np.zeros(384, dtype=np.float32)
        for w in re.findall(r"[a-z]+", t.lower()):
            v[int(hashlib.md5(w.encode()).hexdigest(), 16) % 384] += 1
        n = np.linalg.norm(v)
        out.append(v / n if n else v)
    return out


onnx.ONNXMiniLM_L6_V2.__call__ = _stand_in_embedder

import chromadb  # noqa: E402
from chromadb.utils import embedding_functions  # noqa: E402
from mcp import Client  # noqa: E402

import index  # noqa: E402
import mcp_server  # noqa: E402
import search  # noqa: E402
from conftest import TEST_INDEX_DIR  # noqa: E402

SAMPLE = ROOT / "sample"
PROFILES = [json.loads(line) for line in (SAMPLE / "profiles.jsonl").open(encoding="utf-8") if line.strip()]
META = {p["id"]: p["metadata"] for p in PROFILES}
CLIENT_STAFF = [pid for pid, m in META.items() if m["client_staff"]]


def fingerprint(folder: Path) -> dict[str, str]:
    return {str(p.relative_to(folder)): hashlib.sha256(p.read_bytes()).hexdigest()
            for p in sorted(folder.rglob("*")) if p.is_file() and "out" not in p.relative_to(folder).parts}


@pytest.fixture(scope="session", autouse=True)
def test_index():
    col = chromadb.PersistentClient(path=TEST_INDEX_DIR).get_or_create_collection(
        "candidates", embedding_function=embedding_functions.DefaultEmbeddingFunction(),
        configuration={"hnsw": {"space": "cosine", "ef_search": 500}})
    index.upsert_profiles(col, PROFILES)
    del col
    yield
    mcp_server._collection.cache_clear()
    search.close_temp_copies()
    shutil.rmtree(TEST_INDEX_DIR, ignore_errors=True)


def run(coro_fn):
    async def go():
        async with Client(mcp_server.mcp) as c:
            return await coro_fn(c)
    return asyncio.run(go())


def text(result) -> str:
    return " ".join(getattr(x, "text", "") for x in result.content)


# ---------------------------------------------------------------------------- the agreed interface
def test_exposes_exactly_the_agreed_tools_resource_and_prompt():
    async def body(c):
        return (await c.list_tools(), await c.list_resource_templates(), await c.list_prompts())
    tools, templates, prompts = run(body)
    assert sorted(t.name for t in tools.tools) == ["get_profile", "list_roles", "search_candidates"]
    assert all(t.annotations.read_only_hint and not t.annotations.destructive_hint for t in tools.tools)
    assert all(t.description and len(t.description) > 80 for t in tools.tools)
    assert [t.uri_template for t in templates.resource_templates] == ["jd://{role}"]
    assert [p.name for p in prompts.prompts] == ["shortlist_for_role"]


def test_server_defaults_to_the_sample():
    assert mcp_server.MODE == "sample" and mcp_server.DATA == SAMPLE
    r = run(lambda c: c.call_tool("list_roles", {}))
    assert not r.is_error and r.structured_content["mode"] == "sample"
    assert [x["role"] for x in r.structured_content["roles"]] == ["credit-controller", "semi-senior"]
    assert all(x["jd_uri"] == f"jd://{x['role']}" and x["summary"] for x in r.structured_content["roles"])


# ---------------------------------------------------------------------------- normal behaviour
def test_search_returns_ranked_snippets_inside_the_hard_filters():
    r = run(lambda c: c.call_tool("search_candidates", {"role": "semi-senior", "query": "AAT semi senior", "n_results": 5}))
    sc = r.structured_content
    assert not r.is_error and [h["rank"] for h in sc["results"]] == [1, 2, 3, 4, 5]
    expected_pool = {pid for pid, m in META.items() if search.python_filter(m, "loc_area_wide")}
    assert sc["pool_size"] == len(expected_pool)
    assert all(h["id"] in expected_pool and len(h["snippet"]) <= 300 for h in sc["results"])


def test_qualification_filter_narrows_the_pool():
    r = run(lambda c: c.call_tool("search_candidates", {"role": "semi-senior", "query": "accountant",
                                                        "filters": {"aca_acca_in_progress": True}, "n_results": 20}))
    sc = r.structured_content
    assert not r.is_error and sc["filters_applied"] == {"aca_acca_in_progress": True}
    assert sc["results"] and all(META[h["id"]]["aca_acca_in_progress"] for h in sc["results"])


def test_client_staff_are_never_returned():
    async def body(c):
        hits = []
        for role in ("credit-controller", "semi-senior"):
            r = await c.call_tool("search_candidates", {"role": role, "query": "credit controller accountant", "n_results": 20})
            hits += [h["id"] for h in r.structured_content["results"]]
        profiles = [await c.call_tool("get_profile", {"candidate_id": pid}) for pid in CLIENT_STAFF]
        return hits, profiles
    hits, profiles = run(body)
    assert CLIENT_STAFF and not set(hits) & set(CLIENT_STAFF)
    assert all(p.is_error and "No candidate with id" in text(p) for p in profiles)


def test_get_profile_accepts_text_and_numeric_ids():
    async def body(c):
        return await c.call_tool("get_profile", {"candidate_id": "10"}), await c.call_tool("get_profile", {"candidate_id": 10})
    a, b = run(body)
    assert not a.is_error and not b.is_error and a.structured_content == b.structured_content
    assert a.structured_content["profile"].startswith("Current role:")


def test_job_description_resource():
    r = run(lambda c: c.read_resource("jd://semi-senior"))
    assert r.contents[0].text == (SAMPLE / "jds" / "semi-senior.txt").read_text(encoding="utf-8")


def test_shortlist_prompt_carries_the_rules():
    r = run(lambda c: c.get_prompt("shortlist_for_role", {"role": "credit-controller", "how_many": "2"}))
    body = r.messages[0].content.text
    assert "jd://credit-controller" in body and "get_profile" in body and "no false positives" in body


# ---------------------------------------------------------------------------- guards
@pytest.mark.parametrize("tool,args,expected", [
    ("search_candidates", {"role": "semi-senior", "query": "x", "n_results": 21}, "less than or equal to 20"),
    ("search_candidates", {"role": "semi-senior", "query": "x", "n_results": 0}, "greater than or equal to 1"),
    ("search_candidates", {"role": "astronaut", "query": "x"}, "Unknown role"),
    ("search_candidates", {"role": "semi-senior", "query": "x", "filters": {"likes_cats": True}}, "Extra inputs"),
    ("search_candidates", {"role": "semi-senior", "query": "x", "filters": {"cicm": "yes"}}, "valid boolean"),
    ("search_candidates", {"role": "semi-senior", "query": "   "}, "query is empty"),
    ("search_candidates", {"role": "semi-senior", "query": "x" * 501}, "at most 500"),
    ("get_profile", {"candidate_id": "99999"}, "No candidate with id"),
])
def test_bad_tool_input_gives_a_clear_error(tool, args, expected):
    r = run(lambda c: c.call_tool(tool, args))
    assert r.is_error and expected.lower() in text(r).lower()


def test_unknown_role_advert_and_bad_prompt_arguments_are_errors():
    async def body(c):
        errors = []
        for call in (lambda: c.read_resource("jd://astronaut"),
                     lambda: c.get_prompt("shortlist_for_role", {"role": "astronaut"}),
                     lambda: c.get_prompt("shortlist_for_role", {"role": "semi-senior", "how_many": "50"})):
            try:
                await call()
                errors.append(None)
            except Exception as e:
                errors.append(str(e))
        return errors
    errors = run(body)
    assert "No job advert" in errors[0] and "Unknown role" in errors[1] and "between 1 and 20" in errors[2]


def test_missing_index_says_what_to_run(monkeypatch, tmp_path):
    monkeypatch.setattr(search, "CHROMA_DIR", tmp_path / "nothing_here")
    mcp_server._collection.cache_clear()
    try:
        r = run(lambda c: c.call_tool("search_candidates", {"role": "semi-senior", "query": "x"}))
        assert r.is_error and "Run: python src/index.py" in text(r)
    finally:
        mcp_server._collection.cache_clear()


def test_read_only_sample_and_index_unchanged_by_a_session():
    before_sample, before_index = fingerprint(SAMPLE), fingerprint(Path(TEST_INDEX_DIR))

    async def body(c):
        await c.call_tool("list_roles", {})
        await c.call_tool("search_candidates", {"role": "credit-controller", "query": "credit control", "n_results": 20})
        await c.call_tool("get_profile", {"candidate_id": "1"})
        await c.read_resource("jd://credit-controller")
    run(body)
    assert fingerprint(SAMPLE) == before_sample
    assert fingerprint(Path(TEST_INDEX_DIR)) == before_index

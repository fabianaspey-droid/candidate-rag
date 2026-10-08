"""Tests for the Step 7 agent loop with a scripted fake Claude and a fake collection. No API calls."""
import sys
from pathlib import Path
from types import SimpleNamespace as NS

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
import agent  # noqa: E402

TEXTS = {
    "1": "Current role: Credit Controller, Acme Practice LLP (Jan 2020 - present). Location: Haverleigh.",
    "2": "Current role: Credit Controller, Widget Ltd (Mar 2021 - present). Location: Haverleigh.",
    "3": "Current role: Semi Senior Accountant, Smith & Co (Feb 2026 - present). Location: Haverleigh.",
}


class FakeCollection:
    def __init__(self):
        self.wheres = []

    def query(self, query_texts, n_results, where, include):
        self.wheres.append(where)
        ids = ["1", "2"][:n_results]
        return {"ids": [ids], "distances": [[0.1 * (i + 1) for i in range(len(ids))]]}


def tool(name, inp, tid):
    return NS(type="tool_use", name=name, input=inp, id=tid)


def resp(*blocks):
    return NS(content=list(blocks), stop_reason="tool_use", usage=NS(input_tokens=1000, output_tokens=100))


class FakeClient:
    def __init__(self, script):
        self.script, self.calls = list(script), 0
        self.messages = NS(create=self.create)

    def create(self, **kw):
        self.calls += 1
        return self.script.pop(0)


def test_full_session_with_guards(monkeypatch):
    monkeypatch.setattr(agent, "JDS", Path(__file__).resolve().parents[1] / "sample" / "jds")
    script = [
        resp(tool("search_candidates", {"query": "credit controller", "n_results": 2}, "a"),
             tool("search_candidates", {"query": "accounts receivable", "n_results": 1,
                                        "filters": {"cicm": True, "client_staff": True}}, "b")),
        resp(tool("get_profile", {"id": "2"}, "c"), tool("get_profile", {"id": "3"}, "d")),
        resp(tool("submit_shortlist", {"candidates": [
            {"id": "1", "score": 90, "tier": "strong", "reasoning": "r", "evidence": "Credit Controller, Acme Practice LLP"},
            {"id": "2", "score": 80, "tier": "strong", "reasoning": "r", "evidence": "ten years at a top practice"},
            {"id": "3", "score": 70, "tier": "strong", "reasoning": "r", "evidence": "Semi Senior Accountant, Smith & Co"},
        ]}, "e")),
    ]
    col = FakeCollection()
    out = agent.run_role(FakeClient(script), col, "credit-controller", TEXTS, "claude-haiku-5-5")
    assert out["status"] == "submitted" and out["usage"]["turns"] == 3
    # Claude may narrow with qualification flags but cannot touch the hard filters
    assert "client_staff" not in str(col.wheres[1]).replace("'client_staff': {'$eq': False}", "")
    gets = [e for e in out["trace"] if e.get("tool") == "get_profile"]
    assert gets[0]["error"] is None and "not been returned" in gets[1]["error"]
    tiers = {c["id"]: c["tier"] for c in out["candidates"]}
    assert tiers == {"1": "strong", "2": "possible"}          # 2 downgraded: unverified quote
    assert out["guards"]["dropped_not_retrieved"] == ["3"]   # 3 never came back from a search


def test_turn_limit_writes_no_shortlist(monkeypatch):
    monkeypatch.setattr(agent, "JDS", Path(__file__).resolve().parents[1] / "sample" / "jds")
    script = [resp(tool("search_candidates", {"query": f"q{i}", "n_results": 2}, f"t{i}"))
              for i in range(agent.MAX_TURNS)]
    out = agent.run_role(FakeClient(script), FakeCollection(), "credit-controller", TEXTS, "claude-haiku-5-5")
    assert out["status"] == "aborted: turn limit" and "candidates" not in out
    assert out["usage"]["turns"] == agent.MAX_TURNS

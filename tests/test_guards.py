"""Tests for the Step 5 guards in src/rerank.py. No API calls."""
import json
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from rerank import apply_guards, evidence_verified, parse_reply  # noqa: E402

TEXTS = {
    "1": "Current role: Credit Controller, Acme Practice LLP (Jan 2020 - present). Qualifications: CICM.",
    "2": "Current role: Semi Senior Accountant, Smith & Co (Feb 2026 - present). Qualifications: AAT qualified.",
}
RETRIEVED = ["1", "2"]


def item(pid, tier="strong", score=80, evidence="Credit Controller, Acme Practice LLP"):
    return {"id": pid, "score": score, "tier": tier, "reasoning": "r", "evidence": evidence}


def test_evidence_must_be_verbatim():
    assert evidence_verified("Credit Controller, Acme Practice LLP", TEXTS["1"])
    assert not evidence_verified("Credit Controller at a top-ten practice", TEXTS["1"])


def test_evidence_normalises_only_whitespace_case_and_quote_marks():
    assert evidence_verified("credit  controller,\nACME practice llp", TEXTS["1"])


def test_evidence_too_short_is_unverified():
    assert not evidence_verified("CICM", TEXTS["1"])


def test_id_not_retrieved_is_dropped():
    out, log = apply_guards([item("1"), item("999")], RETRIEVED, TEXTS)
    assert [r["id"] for r in out] == ["1"]
    assert log["dropped_not_retrieved"] == ["999"]


def test_strong_on_unverified_quote_is_downgraded():
    out, log = apply_guards([item("1", evidence="Ten years at a top-ten firm")], RETRIEVED, TEXTS)
    assert out[0]["tier"] == "possible" and out[0]["tier_from_claude"] == "strong"
    assert log["downgraded_unverified"] == ["1"]


def test_duplicate_and_missing_are_logged():
    out, log = apply_guards([item("1"), item("1")], RETRIEVED, TEXTS)
    assert len(out) == 1 and log["dropped_duplicate"] == ["1"] and log["missing_from_reply"] == ["2"]


def test_malformed_tier_or_score_is_dropped():
    out, log = apply_guards([item("1", tier="maybe"), item("2", score=150,
                             evidence="Semi Senior Accountant, Smith & Co")], RETRIEVED, TEXTS)
    assert out == [] and set(log["dropped_malformed"]) == {"1", "2"}


def test_unparseable_reply_raises_so_nothing_is_written():
    with pytest.raises(ValueError):
        parse_reply('{"candidates": [ {"id": "1", ')
    with pytest.raises(ValueError):
        parse_reply('{"shortlist": []}')


def test_fenced_json_parses():
    assert parse_reply("```json\n" + json.dumps({"candidates": [item("1")]}) + "\n```")[0]["id"] == "1"

"""
src/rerank.py  -  Step 5 of candidate-rag: Claude re-ranks the retrieved 20, with guards.

For each role: the advert and ONLY the 20 candidates retrieved by hybrid_rrf in Step 4 go to Claude
in one call. Claude returns JSON: for each candidate an id, a score out of 100, a tier (strong,
possible, reject), one or two sentences of reasoning, and an evidence quote copied from the profile.

Guards (same spirit as the Gini provenance guards):
  * the reply must parse as JSON with the expected shape, or NOTHING is written for that role;
  * any id that was not in the retrieved set is dropped and logged;
  * duplicate ids: the first is kept, the rest dropped and logged;
  * every evidence quote is checked word for word against that candidate's own profile text
    (whitespace and quote marks normalised, nothing else), and must be at least 15 characters;
  * a candidate can never be strong on an unverified quote: it is downgraded to possible and flagged;
  * retrieved candidates Claude did not return are logged as missing.

Cost control: without --spend this is a DRY RUN. It builds the prompts, counts input tokens with the
free token-counting endpoint, and prints the estimate. Nothing is charged. With --spend it makes one
call per role, with max_tokens capping the output, and logs the real tokens and cost from the
API's own usage figures.

Usage (from the candidate-rag folder, venv active):
    python src/rerank.py            # dry run: estimate only, no charge
    python src/rerank.py --spend    # the real run
"""

from __future__ import annotations

import argparse
import json
import re
import sys
import time
from pathlib import Path

from anthropic import Anthropic
from dotenv import load_dotenv

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DATA, JDS, OUT, PROJECT  # noqa: E402
METHOD = "hybrid_rrf"
MODEL = "claude-haiku-5-5"
MAX_TOKENS = 8000
MIN_QUOTE_CHARS = 15
# v1 (08/10 17:15): the credit-controller rule named professional services as "the best fit" without
#   saying industry credit controllers can be strong. sourcing-core's own rule (the client config:
#   employers preferred practice/law firm, ACCEPTED industry) allows them. Claude kept 0 of the 5
#   answer-key-strong credit controllers it was given as strong. v1 outputs are kept in data/archive_prompt_v1.
# v2: states that B2B credit controllers can be strong in any industry; professional services scores higher.
PROMPT_VERSION = "v2"
# USD per million tokens, prompts under 100k tokens. Checked 08/10/2026 on
# platform.claude.com/docs/en/about-claude/pricing.
PRICES = {"claude-haiku-5-5": (0.10, 0.50), "claude-haiku-4-5-20251001": (1.00, 5.00)}

ROLES = {"credit-controller": "credit-controller.txt", "semi-senior": "semi-senior.txt"}

SYSTEM = """You are a careful recruitment consultant screening candidates for a UK accountancy practice.
You will receive one job advert and a set of candidate profiles. Assess every candidate against the advert.

Rules, agreed with the recruiter:
- Favour no false positives over coverage. If in doubt, do not mark a candidate strong.
- Be cautious on qualifications. Only count a qualification the profile states. "Likely held" or an
  unspecified level is weaker evidence than "qualified".
- A candidate with the right job title but no visible qualification, where the role asks for one, is
  possible, not strong.
- For the Credit Controller role: no qualification is required. The advert asks for credit control
  experience (or someone eager to step into a first finance role). A current or recent credit controller
  with business-to-business credit control experience can be strong in any industry. Relationship-led,
  partner-facing credit control in professional services (billing, WIP) is the best fit and should score
  highest, but it is a preference, not a requirement for strong. Consumer or finance-house collections
  (credit unions, vehicle or asset finance, lenders, debt recovery agencies) are volume chasing, the
  opposite of the advert: possible at most. Credit manager or credit control manager titles are possibly
  too senior: possible at most.
- Judge only from the profile text. Do not invent facts.

Return ONLY a JSON object, no other text, in exactly this shape:
{"candidates": [{"id": "<candidate id>", "score": <integer 0-100>, "tier": "strong" | "possible" | "reject",
  "reasoning": "<one or two sentences>", "evidence": "<a phrase copied exactly from that candidate's profile>"}]}
Include every candidate exactly once. The evidence must be copied character for character from the
candidate's own profile text, at least a few words long, and must support your reasoning."""


# ---------------------------------------------------------------------------- guards (pure, tested)
def _norm(s: str) -> str:
    s = s.replace("’", "'").replace("‘", "'").replace("“", '"').replace("”", '"')
    s = s.replace("–", "-").replace("—", "-")
    return " ".join(s.split()).lower()


def evidence_verified(quote: str, profile_text: str) -> bool:
    q = _norm(quote or "")
    return len(q) >= MIN_QUOTE_CHARS and q in _norm(profile_text)


def parse_reply(text: str) -> list[dict]:
    """Parse Claude's reply. Raises ValueError on anything that isn't the expected shape."""
    t = text.strip()
    fence = re.match(r"^```(?:json)?\s*(.*?)\s*```$", t, re.S)
    if fence:
        t = fence.group(1)
    data = json.loads(t)  # raises ValueError (JSONDecodeError) if it won't parse
    if not isinstance(data, dict) or not isinstance(data.get("candidates"), list):
        raise ValueError("reply is not an object with a 'candidates' list")
    return data["candidates"]


def apply_guards(items: list[dict], retrieved: list[str], texts: dict[str, str]) -> tuple[list[dict], dict]:
    log = {"dropped_not_retrieved": [], "dropped_duplicate": [], "dropped_malformed": [],
           "downgraded_unverified": [], "unverified_evidence": [], "missing_from_reply": []}
    allowed, seen, out = set(retrieved), set(), []
    for it in items:
        pid = str(it.get("id", "")).strip() if isinstance(it, dict) else ""
        if not pid or not isinstance(it, dict):
            log["dropped_malformed"].append(repr(it)[:80])
            continue
        if pid not in allowed:
            log["dropped_not_retrieved"].append(pid)
            continue
        if pid in seen:
            log["dropped_duplicate"].append(pid)
            continue
        tier = str(it.get("tier", "")).strip().lower()
        try:
            score = int(it.get("score"))
        except (TypeError, ValueError):
            score = None
        if tier not in ("strong", "possible", "reject") or score is None or not 0 <= score <= 100:
            log["dropped_malformed"].append(pid)
            continue
        seen.add(pid)
        ok = evidence_verified(str(it.get("evidence", "")), texts[pid])
        rec = {"id": pid, "score": score, "tier": tier, "tier_from_claude": tier,
               "reasoning": str(it.get("reasoning", "")).strip(), "evidence": str(it.get("evidence", "")),
               "evidence_verified": ok}
        if not ok:
            log["unverified_evidence"].append(pid)
            if tier == "strong":
                rec["tier"] = "possible"
                log["downgraded_unverified"].append(pid)
        out.append(rec)
    log["missing_from_reply"] = [pid for pid in retrieved if pid not in seen and pid not in log["dropped_malformed"]]
    out.sort(key=lambda r: (-r["score"], int(r["id"])))
    return out, log


# ---------------------------------------------------------------------------- prompt
def build_prompt(advert: str, retrieved: list[str], texts: dict[str, str]) -> str:
    cands = "\n".join(f'<candidate id="{pid}">\n{texts[pid]}\n</candidate>' for pid in retrieved)
    return f"<job_advert>\n{advert.strip()}\n</job_advert>\n\n<candidates>\n{cands}\n</candidates>"


def cost_usd(model: str, tokens_in: int, tokens_out: int) -> float:
    pin, pout = PRICES[model]
    return tokens_in * pin / 1e6 + tokens_out * pout / 1e6


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spend", action="store_true", help="make the real API calls (costs money)")
    ap.add_argument("--model", default=MODEL, choices=sorted(PRICES))
    ap.add_argument("--run", type=int, default=1,
                    help="run number; run 1 writes rerank_<role>.json, run N writes rerank_<role>_runN.json")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    load_dotenv(PROJECT / ".env")
    client = Anthropic()

    profiles = [json.loads(l) for l in (DATA / "profiles.jsonl").open(encoding="utf-8") if l.strip()]
    texts = {p["id"]: p["text"] for p in profiles}

    print("=" * 70)
    print(f"STEP 5 {'RUN' if args.spend else 'DRY RUN (no charge)'}  model {args.model}  prompt {PROMPT_VERSION}  (safe to paste)")
    print("=" * 70)
    total = 0.0
    for role, jd in ROLES.items():
        retr = json.loads((OUT / f"retrieval_{role}.json").read_text(encoding="utf-8"))
        retrieved = [r["id"] for r in retr["methods"][METHOD]]
        advert = (JDS / jd).read_text(encoding="utf-8")
        prompt = build_prompt(advert, retrieved, texts)
        print("")
        print(f"### {role}: {len(retrieved)} candidates from {METHOD}")

        if not args.spend:
            try:
                n_in = client.messages.count_tokens(model=args.model, system=SYSTEM,
                                                    messages=[{"role": "user", "content": prompt}]).input_tokens
                basis = "counted by the API (free endpoint)"
            except Exception as e:
                n_in = (len(SYSTEM) + len(prompt)) // 4
                basis = f"estimated at 4 characters per token (count_tokens failed: {type(e).__name__})"
            est_out = 25 * 120  # ~120 output tokens per candidate, assumed
            worst = cost_usd(args.model, n_in, MAX_TOKENS)
            est = cost_usd(args.model, n_in, est_out)
            total += worst
            print(f"input tokens: {n_in} ({basis})")
            print(f"output: assumed ~{est_out} tokens, capped at {MAX_TOKENS} (any thinking counts as output)")
            print(f"cost: about ${est:.4f}; worst case at the cap ${worst:.4f}")
            continue

        t0 = time.time()
        resp = client.messages.create(model=args.model, max_tokens=MAX_TOKENS, system=SYSTEM,
                                      messages=[{"role": "user", "content": prompt}])
        secs = time.time() - t0
        u = resp.usage
        c = cost_usd(args.model, u.input_tokens, u.output_tokens)
        total += c
        print(f"tokens in {u.input_tokens}, out {u.output_tokens}; cost ${c:.4f}; {secs:.1f}s; "
              f"stop_reason {resp.stop_reason}")
        reply = "".join(b.text for b in resp.content if getattr(b, "type", "") == "text")
        raw_path = OUT / (f"rerank_raw_{role}.txt" if args.run == 1 else f"rerank_raw_{role}_run{args.run}.txt")
        raw_path.write_text(reply, encoding="utf-8")
        try:
            items = parse_reply(reply)
        except ValueError as e:
            print(f"GUARD: reply did not parse ({e}). NOTHING WRITTEN for {role}. Raw reply kept in {raw_path.name}.")
            continue
        shortlist, log = apply_guards(items, retrieved, texts)
        out = {"role": role, "model": args.model, "prompt_version": PROMPT_VERSION, "method": METHOD,
               "retrieved": retrieved,
               "usage": {"input_tokens": u.input_tokens, "output_tokens": u.output_tokens,
                         "cost_usd": round(c, 6), "seconds": round(secs, 1)},
               "guards": log, "candidates": shortlist}
        out_name = f"rerank_{role}.json" if args.run == 1 else f"rerank_{role}_run{args.run}.json"
        (OUT / out_name).write_text(json.dumps(out, indent=2), encoding="utf-8")
        tiers = {t: sum(1 for r in shortlist if r["tier"] == t) for t in ("strong", "possible", "reject")}
        print(f"returned {len(items)}; kept {len(shortlist)}; tiers after guards {tiers}")
        print("guards: " + "; ".join(f"{k} {len(v)}" + (f" {v}" if v else "") for k, v in log.items()))
        print(f"evidence verified: {sum(r['evidence_verified'] for r in shortlist)} of {len(shortlist)}")
        print("strong after guards (id, score): " + ", ".join(
            f"{r['id']} ({r['score']})" for r in shortlist if r["tier"] == "strong"))
        print(f"saved: data/{out_name}")
    print("")
    print(f"TOTAL: {'worst case $' if not args.spend else '$'}{total:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

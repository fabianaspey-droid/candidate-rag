"""
src/agent.py  -  Step 7 of candidate-rag: agentic search with Claude tool use.

Step 5 is a workflow: the code decides what to retrieve and Claude only re-ranks it. Here Claude
decides what to search for, reads the results, searches again from other angles, and decides when it
has enough. Three tools through the Messages API:

  search_candidates  query (text), n_results (1-20), optional filters on qualification flags.
                     The hard filters (not client staff; role's area or "United Kingdom" only) are
                     ALWAYS added by the code; Claude can only narrow further, never widen.
                     Returns ids, distances and a short snippet.
  get_profile        one candidate's full profile text, by id (only ids a search returned this run).
  submit_shortlist   the final answer; ends the run.

Limits: at most MAX_TURNS API calls and an input-token budget. If either is hit: stop, write NO
shortlist (the trace is still saved, marked aborted).

Guards on the submitted shortlist (the same code as Step 5): every id must have come back from a
search in this run; every evidence quote is checked word for word against that candidate's profile;
strong on an unverified quote is downgraded to possible.

Every tool call is saved to data/agent_trace_<role>.json (turn, tool, query, filters, result count,
ids returned) with tokens per turn. The shortlist goes to data/agent_<role>.json for evaluate.py.

Usage (from the candidate-rag folder, venv active):
    python src/agent.py            # prints the cost ceiling only, no charge
    python src/agent.py --spend    # the real run, both roles
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from index import CHROMA_DIR, COLLECTION, load_profiles  # noqa: E402
from rerank import PRICES, SYSTEM as RERANK_RULES, apply_guards, cost_usd  # noqa: E402
from retrieve import JDS, ROLES as RETRIEVE_ROLES, chroma_where  # noqa: E402

from paths import OUT, PROJECT  # noqa: E402
MODEL = "claude-haiku-5-5"
MAX_TURNS = 8
INPUT_TOKEN_BUDGET = 120_000   # cumulative input tokens per role across all turns
MAX_TOKENS = 8000              # output cap per call
SNIPPET_CHARS = 300
FILTER_FLAGS = ["aat_qualified", "aca_acca_held", "aca_acca_in_progress", "cicm", "has_any_qual"]

TOOLS = [
    {
        "name": "search_candidates",
        "description": ("Semantic search over the candidate pool for this role. The location and client-staff "
                        "filters are always applied for you. Returns up to n_results candidates as id, distance "
                        "(lower is closer) and the start of their profile."),
        "input_schema": {
            "type": "object",
            "properties": {
                "query": {"type": "string", "description": "What to search for, in plain words."},
                "n_results": {"type": "integer", "minimum": 1, "maximum": 20},
                "filters": {
                    "type": "object",
                    "description": "Optional. Only return candidates whose qualification flags match.",
                    "properties": {f: {"type": "boolean"} for f in FILTER_FLAGS},
                    "additionalProperties": False,
                },
            },
            "required": ["query", "n_results"],
        },
    },
    {
        "name": "get_profile",
        "description": "The full profile text of one candidate. Only ids returned by a search in this session.",
        "input_schema": {"type": "object", "properties": {"id": {"type": "string"}}, "required": ["id"]},
    },
    {
        "name": "submit_shortlist",
        "description": ("Submit your final assessment. This ends the session. Include every candidate you "
                        "assessed, each with a tier, a score, one or two sentences of reasoning and an evidence "
                        "phrase copied exactly from that candidate's profile."),
        "input_schema": {
            "type": "object",
            "properties": {
                "candidates": {
                    "type": "array",
                    "items": {
                        "type": "object",
                        "properties": {
                            "id": {"type": "string"},
                            "score": {"type": "integer", "minimum": 0, "maximum": 100},
                            "tier": {"type": "string", "enum": ["strong", "possible", "reject"]},
                            "reasoning": {"type": "string"},
                            "evidence": {"type": "string"},
                        },
                        "required": ["id", "score", "tier", "reasoning", "evidence"],
                    },
                }
            },
            "required": ["candidates"],
        },
    },
]


def system_prompt(advert: str) -> str:
    rules = RERANK_RULES.split("Return ONLY a JSON object")[0].strip()
    return (rules + "\n\nYou have tools to search the candidate pool yourself. Work like a sourcer:\n"
            "- Search from several genuinely different angles before deciding: the job title and its synonyms, "
            "the qualifications the advert asks for, and adjacent roles that could fit.\n"
            "- Read full profiles with get_profile when the snippet is not enough to judge.\n"
            "- You have a limited number of turns. You may call several tools in one turn.\n"
            "- When you have enough, call submit_shortlist once with every candidate you assessed.\n\n"
            f"<job_advert>\n{advert.strip()}\n</job_advert>")


class Session:
    """Runs the tools for one role and records the trace."""

    def __init__(self, col, area_flag: str, texts: dict[str, str]):
        self.col, self.area_flag, self.texts = col, area_flag, texts
        self.seen: list[str] = []
        self.trace: list[dict] = []

    def search(self, query: str, n_results: int, filters: dict | None = None) -> dict:
        n = max(1, min(int(n_results), 20))
        clauses = [chroma_where(self.area_flag)]
        used = {}
        for k, v in (filters or {}).items():
            if k in FILTER_FLAGS and isinstance(v, bool):
                clauses.append({k: {"$eq": v}})
                used[k] = v
        where = clauses[0] if len(clauses) == 1 else {"$and": clauses}
        res = self.col.query(query_texts=[query], n_results=n, where=where, include=["distances"])
        out = []
        for pid, d in zip(res["ids"][0], res["distances"][0]):
            if pid not in self.seen:
                self.seen.append(pid)
            out.append({"id": pid, "distance": round(d, 3), "snippet": self.texts[pid][:SNIPPET_CHARS]})
        return {"filters_applied": used, "results": out}

    def get_profile(self, pid: str) -> dict:
        pid = str(pid)
        if pid not in self.seen:
            return {"error": f"id {pid} has not been returned by any search in this session"}
        return {"id": pid, "profile": self.texts[pid]}


def run_role(client, col, role: str, texts: dict[str, str], model: str) -> dict:
    cfg = RETRIEVE_ROLES[role]
    advert = (JDS / cfg["jd"]).read_text(encoding="utf-8")
    sess = Session(col, cfg["area_flag"], texts)
    messages = [{"role": "user", "content": "Find the best candidates for this role."}]
    usage = {"input_tokens": 0, "output_tokens": 0, "turns": 0}
    status, submitted = "aborted: turn limit", None

    for turn in range(1, MAX_TURNS + 1):
        resp = client.messages.create(model=model, max_tokens=MAX_TOKENS, system=system_prompt(advert),
                                      tools=TOOLS, messages=messages)
        usage["turns"] = turn
        usage["input_tokens"] += resp.usage.input_tokens
        usage["output_tokens"] += resp.usage.output_tokens
        sess.trace.append({"turn": turn, "event": "api_call", "input_tokens": resp.usage.input_tokens,
                           "output_tokens": resp.usage.output_tokens, "stop_reason": resp.stop_reason})
        messages.append({"role": "assistant", "content": resp.content})

        tool_uses = [b for b in resp.content if getattr(b, "type", "") == "tool_use"]
        if not tool_uses:
            messages.append({"role": "user", "content": "Please continue with the tools, and finish by calling submit_shortlist."})
        results = []
        for tu in tool_uses:
            entry = {"turn": turn, "tool": tu.name}
            if tu.name == "search_candidates":
                out = sess.search(tu.input.get("query", ""), tu.input.get("n_results", 10), tu.input.get("filters"))
                entry.update({"query": tu.input.get("query"), "filters": out["filters_applied"],
                              "n_results": tu.input.get("n_results"), "result_ids": [r["id"] for r in out["results"]]})
            elif tu.name == "get_profile":
                out = sess.get_profile(tu.input.get("id", ""))
                entry.update({"id": str(tu.input.get("id", "")), "error": out.get("error")})
            elif tu.name == "submit_shortlist":
                submitted = tu.input.get("candidates", [])
                out = {"ok": True}
                entry["n_candidates"] = len(submitted) if isinstance(submitted, list) else None
            else:
                out = {"error": f"unknown tool {tu.name}"}
            sess.trace.append(entry)
            results.append({"type": "tool_result", "tool_use_id": tu.id, "content": json.dumps(out)})
        if submitted is not None:
            status = "submitted"
            break
        if results:
            messages.append({"role": "user", "content": results})
        if usage["input_tokens"] > INPUT_TOKEN_BUDGET:
            status = "aborted: token budget"
            break

    usage["cost_usd"] = round(cost_usd(model, usage["input_tokens"], usage["output_tokens"]), 6)
    out = {"role": role, "model": model, "status": status, "usage": usage,
           "seen_ids": sess.seen, "trace": sess.trace}
    if status == "submitted" and isinstance(submitted, list):
        shortlist, log = apply_guards(submitted, sess.seen, texts)
        log["missing_from_reply"] = []  # the agent only submits what it assessed; not a fault here
        out.update({"guards": log, "candidates": shortlist})
    return out


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--spend", action="store_true", help="make the real API calls (costs money)")
    ap.add_argument("--model", default=MODEL, choices=sorted(PRICES))
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    ceiling = cost_usd(args.model, INPUT_TOKEN_BUDGET + 20_000, MAX_TURNS * MAX_TOKENS)
    print("=" * 70)
    print(f"STEP 7 AGENT {'RUN' if args.spend else '(no charge: limits only)'}  model {args.model}  (safe to paste)")
    print("=" * 70)
    print(f"limits per role: {MAX_TURNS} turns, input budget {INPUT_TOKEN_BUDGET:,} tokens (checked after each "
          f"turn, so one turn can overshoot), output cap {MAX_TOKENS:,} per turn")
    print(f"hard ceiling per role about ${ceiling:.3f}; two roles about ${2 * ceiling:.3f}")
    if not args.spend:
        return 0

    import chromadb
    from anthropic import Anthropic
    from chromadb.utils import embedding_functions
    from dotenv import load_dotenv
    load_dotenv(PROJECT / ".env")
    client = Anthropic()
    col = chromadb.PersistentClient(path=str(CHROMA_DIR)).get_collection(
        COLLECTION, embedding_function=embedding_functions.DefaultEmbeddingFunction())
    texts = {p["id"]: p["text"] for p in load_profiles()}

    total = 0.0
    for role in RETRIEVE_ROLES:
        t0 = time.time()
        res = run_role(client, col, role, texts, args.model)
        res["usage"]["seconds"] = round(time.time() - t0, 1)
        total += res["usage"]["cost_usd"]
        (OUT / f"agent_trace_{role}.json").write_text(json.dumps(
            {k: res[k] for k in ("role", "model", "status", "usage", "seen_ids", "trace")}, indent=2, default=str),
            encoding="utf-8")
        u = res["usage"]
        print("")
        print(f"### {role}: {res['status']}; {u['turns']} turns; {u['input_tokens']:,} in, {u['output_tokens']:,} out; "
              f"${u['cost_usd']:.4f}; {u['seconds']}s")
        for e in res["trace"]:
            if e.get("tool") == "search_candidates":
                print(f"  turn {e['turn']} search: \"{e['query']}\" n={e['n_results']}"
                      + (f" filters={e['filters']}" if e["filters"] else "") + f" -> {len(e['result_ids'])} results")
        gets = [e for e in res["trace"] if e.get("tool") == "get_profile"]
        print(f"  get_profile calls: {len(gets)} (refused: {sum(1 for e in gets if e.get('error'))}); "
              f"distinct candidates seen: {len(res['seen_ids'])}")
        if res["status"] != "submitted":
            print("  NO SHORTLIST WRITTEN (limit hit). Trace saved.")
            continue
        sl, log = res["candidates"], res["guards"]
        tiers = {t: sum(1 for r in sl if r["tier"] == t) for t in ("strong", "possible", "reject")}
        print(f"  submitted {len(sl)} after guards; tiers {tiers}; evidence verified "
              f"{sum(r['evidence_verified'] for r in sl)} of {len(sl)}")
        print("  guards: " + "; ".join(f"{k} {len(v)}" + (f" {v}" if v else "") for k, v in log.items()))
        print("  strong (id, score): " + ", ".join(f"{r['id']} ({r['score']})" for r in sl if r["tier"] == "strong"))
        (OUT / f"agent_{role}.json").write_text(json.dumps(
            {"role": role, "model": args.model, "method": "agent", "usage": res["usage"],
             "guards": log, "candidates": sl}, indent=2), encoding="utf-8")
    print("")
    print(f"TOTAL: ${total:.4f}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

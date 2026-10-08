"""
src/retrieve.py  -  Step 4 of candidate-rag: for each job description, the 20 closest candidates.

Three methods per role, all inside the same hard filters:

  sections_rrf  The advert split into its sections; the sections that sell the employer (about the
                firm, benefits, sign-off) dropped; each remaining section embedded as its own query
                (each fits the model's 256 word-piece window); the rankings combined with reciprocal
                rank fusion: score = sum over sections of 1 / (60 + rank). THE MAIN METHOD.
  whole_ad      The whole advert as one query. The model silently reads only its first 256 word
                pieces. Kept as a comparison.
  keyword_bm25  Keyword baseline: BM25 over the profile texts, query = the same kept sections.
  hybrid_rrf    Lexical + semantic hybrid: reciprocal rank fusion of two rankings, sections_rrf and
                keyword_bm25, one vote each. Added 08/10 after the first real run: the embedding
                model blurred qualification acronyms (AAT, ACA, ACCA) and seniority, and BM25 caught
                more of the rules-strong semi-seniors. Fixed design, not tuned on the results.

Hard filters (metadata, applied by Chroma's where clause): not the client's own staff, and located in
the role's area or "United Kingdom" only. No qualification filter (decided 08/10).

Leakage guard: ranking uses only Chroma results and profile TEXT. sourcing-core's verdicts are
read only afterwards, for the check printed at the end, and are not stored in Chroma at all.

Writes data/retrieval_<role>.json for Steps 5 and 6. No API calls, no cost.

Usage (from the candidate-rag folder, venv active):
    python src/retrieve.py
"""

from __future__ import annotations

import json
import math
import re
import sys
from collections import Counter
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

sys.path.insert(0, str(Path(__file__).resolve().parent))
from index import COLLECTION, CHROMA_DIR, MODEL_WINDOW, load_profiles, word_piece_counts  # noqa: E402
from paths import JDS, OUT  # noqa: E402
TOP_K = 20
RRF_K = 60

ROLES = {
    "credit-controller": {"jd": "credit-controller.txt", "eval_key": "credit_controller",
                          "area_flag": "loc_area_core"},
    "semi-senior": {"jd": "semi-senior.txt", "eval_key": "semi_senior",
                    "area_flag": "loc_area_wide"},
}

KEEP_HEADINGS = ["the role", "what you'll be doing", "what we're looking for", "nice to have",
                 "where this role can take you", "key responsibilities", "about you"]
DROP_HEADINGS = ["why join", "what's on offer", "interested in joining", "about",
                 "what success looks like"]

STOPWORDS = set("""a an and are as at be been but by can for from has have i if in into is it its
of on or our so that the their them they this to up we what who will with you your youll yourll
re ll ve s t""".split())


# ---------------------------------------------------------------------------- the query side
def _heading(line: str) -> str | None:
    norm = line.strip().lower().replace("’", "'").rstrip(":?").strip()
    if not norm or len(norm) > 40:
        return None
    for h in KEEP_HEADINGS + DROP_HEADINGS:
        if norm.startswith(h):
            return norm
    return None


def split_advert(text: str) -> tuple[str, list[dict]]:
    """Return (role title, sections). Each section: heading, body, keep (bool)."""
    lines = text.replace("\r\n", "\n").split("\n")
    title = lines[0].split("|")[0].strip()
    sections, current = [], {"heading": "intro", "lines": []}
    for line in lines[1:]:
        h = _heading(line)
        if h:
            sections.append(current)
            current = {"heading": h, "lines": []}
        elif line.strip():
            current["lines"].append(line.strip())
    sections.append(current)
    out = []
    for s in sections:
        # bullets end without punctuation; give each one a full stop so they don't run together
        body = " ".join(l if l[-1] in ".!?:;" else l + "." for l in s["lines"])
        if not body:
            continue
        keep = any(s["heading"].startswith(h) for h in KEEP_HEADINGS)
        out.append({"heading": s["heading"], "body": body, "keep": keep})
    return title, out


def section_queries(title: str, sections: list[dict]) -> list[dict]:
    return [{"heading": s["heading"], "text": f"{title}. {s['heading'].capitalize()}: {s['body']}"}
            for s in sections if s["keep"]]


# ---------------------------------------------------------------------------- filters
def chroma_where(area_flag: str) -> dict:
    return {"$and": [{"client_staff": {"$eq": False}},
                     {"$or": [{area_flag: {"$eq": True}}, {"loc_uk_only": {"$eq": True}}]}]}


def python_filter(meta: dict, area_flag: str) -> bool:
    """The same rule as chroma_where, used to check Chroma applied it exactly."""
    return (not meta["client_staff"]) and (bool(meta[area_flag]) or bool(meta["loc_uk_only"]))


# ---------------------------------------------------------------------------- methods
def embed_rank(col, query: str, where: dict, n: int) -> list[tuple[str, float]]:
    res = col.query(query_texts=[query], n_results=n, where=where, include=["distances"])
    return list(zip(res["ids"][0], res["distances"][0]))


def rrf(rankings: list[list[str]], k: int = RRF_K) -> list[tuple[str, float]]:
    scores: Counter = Counter()
    for ranking in rankings:
        for rank, pid in enumerate(ranking, 1):
            scores[pid] += 1.0 / (k + rank)
    return sorted(scores.items(), key=lambda x: (-x[1], int(x[0])))


def tokens(text: str) -> list[str]:
    return [t for t in re.findall(r"[a-z0-9]+", text.lower().replace("'", "")) if t not in STOPWORDS]


def bm25(query: str, docs: dict[str, str], pool: set[str], k1: float = 1.5, b: float = 0.75):
    toks = {pid: tokens(t) for pid, t in docs.items()}
    n = len(toks)
    avgdl = sum(len(t) for t in toks.values()) / n
    df = Counter(term for t in toks.values() for term in set(t))
    q = set(tokens(query))
    out = []
    for pid in pool:
        tf = Counter(toks[pid])
        dl = len(toks[pid])
        s = 0.0
        for term in q:
            if tf[term]:
                idf = math.log(1 + (n - df[term] + 0.5) / (df[term] + 0.5))
                s += idf * tf[term] * (k1 + 1) / (tf[term] + k1 * (1 - b + b * dl / avgdl))
        out.append((pid, s))
    return sorted(out, key=lambda x: (-x[1], int(x[0])))


# ---------------------------------------------------------------------------- main
def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    profiles = load_profiles()
    meta = {p["id"]: p["metadata"] for p in profiles}
    texts = {p["id"]: p["text"] for p in profiles}

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    col = client.get_collection(COLLECTION, embedding_function=embedding_functions.DefaultEmbeddingFunction())

    print("=" * 70)
    print("STEP 4 CHECK  (safe to paste: counts and job titles only)")
    print("=" * 70)
    for role, cfg in ROLES.items():
        advert = (JDS / cfg["jd"]).read_text(encoding="utf-8")
        title, sections = split_advert(advert)
        queries = section_queries(title, sections)
        pool = {pid for pid, m in meta.items() if python_filter(m, cfg["area_flag"])}
        where = chroma_where(cfg["area_flag"])
        n = len(pool)

        # whole pool from Chroma, to prove the where clause matches the Python rule exactly,
        # asked twice to see whether the vector search is repeatable
        whole = embed_rank(col, advert, where, n)
        whole_again = embed_rank(col, advert, where, n)
        chroma_pool = {pid for pid, _ in whole}

        per_section = [embed_rank(col, q["text"], where, n) for q in queries]
        fused = rrf([[pid for pid, _ in r] for r in per_section])
        kw = bm25(" ".join(q["text"] for q in queries), texts, pool)
        hybrid = rrf([[pid for pid, _ in fused], [pid for pid, _ in kw]])

        methods = {
            "sections_rrf": [{"id": pid, "rank": i, "score": round(s, 6)} for i, (pid, s) in enumerate(fused[:TOP_K], 1)],
            "whole_ad": [{"id": pid, "rank": i, "distance": round(d, 6)} for i, (pid, d) in enumerate(whole[:TOP_K], 1)],
            "keyword_bm25": [{"id": pid, "rank": i, "score": round(s, 6)} for i, (pid, s) in enumerate(kw[:TOP_K], 1)],
            "hybrid_rrf": [{"id": pid, "rank": i, "score": round(s, 6)} for i, (pid, s) in enumerate(hybrid[:TOP_K], 1)],
        }
        wp = word_piece_counts([q["text"] for q in queries]) or [None] * len(queries)
        out = {
            "role": role, "jd_file": cfg["jd"], "top_k": TOP_K, "rrf_k": RRF_K,
            "filters": {"client_staff": False, "location": f"{cfg['area_flag']} or loc_uk_only"},
            "pool_size": n,
            "sections": [{"heading": s["heading"], "kept": s["keep"], "words": len(s["body"].split())} for s in sections],
            "queries": [{"heading": q["heading"], "word_pieces": c} for q, c in zip(queries, wp)],
            "methods": methods,
        }
        path = OUT / f"retrieval_{role}.json"
        path.write_text(json.dumps(out, indent=2), encoding="utf-8")

        # ------------------------------------------------------------ the check (labels read here only)
        label = {pid: meta[pid][f"eval_label_{cfg['eval_key']}"] for pid in pool}
        strong_pool = {pid for pid in pool if label[pid] == "strong"}
        possible_pool = {pid for pid in pool if label[pid] == "possible"}
        print("")
        print(f"### {role}")
        print(f"pool after hard filters: {n} of {len(meta)} "
              f"(client staff out: {sum(1 for m in meta.values() if m['client_staff'])}; "
              f"Chroma returned exactly the same pool: {chroma_pool == pool})")
        print(f"  whole-advert search: asked for {n}, returned {len(whole)} ({len(chroma_pool)} distinct); "
              f"missing: {', '.join(sorted(pool - chroma_pool, key=int)) or 'none'}; "
              f"extra: {', '.join(sorted(chroma_pool - pool, key=int)) or 'none'}; "
              f"identical on repeat: {[p for p, _ in whole] == [p for p, _ in whole_again]}")
        print("sections: " + "; ".join(f"{s['heading']} [{'kept' if s['keep'] else 'dropped'}]" for s in sections))
        print("query word pieces: " + ", ".join(f"{q['heading']} {c}" for q, c in zip(queries, wp))
              + (f"  (window {MODEL_WINDOW})" if wp and wp[0] else ""))
        print(f"rules-strong in pool: {len(strong_pool)}; a random {TOP_K} would hold "
              f"{TOP_K * len(strong_pool) / n:.1f} on average")
        tops = {m: {r['id'] for r in rows} for m, rows in methods.items()}
        for m, ids in tops.items():
            print(f"  {m:13} top {TOP_K}: rules-strong {len(ids & strong_pool):2} of {len(strong_pool)}, "
                  f"rules-possible {len(ids & possible_pool):2}")
        print(f"  overlap with sections_rrf: whole_ad {len(tops['whole_ad'] & tops['sections_rrf'])}, "
              f"keyword_bm25 {len(tops['keyword_bm25'] & tops['sections_rrf'])}, "
              f"hybrid_rrf {len(tops['hybrid_rrf'] & tops['sections_rrf'])}")
        missed = sorted(strong_pool - tops["hybrid_rrf"], key=int)
        if len(strong_pool) <= TOP_K:
            print(f"  rules-strong MISSED by hybrid_rrf: {len(missed)}"
                  + "".join(f"\n    id {pid}: {meta[pid]['current_title'] or '(no current role)'} | quals: "
                            f"{meta[pid]['qual_codes'] or 'none'}" for pid in missed))
        print(f"  hybrid_rrf top {TOP_K}:")
        for r in methods["hybrid_rrf"]:
            m = meta[r["id"]]
            print(f"    {r['rank']:2}. id {r['id']:>4}  {m['current_title'] or '(no current role)'}"
                  f" | quals: {m['qual_codes'] or 'none'} | rules: {label[r['id']]}")
        print(f"  saved: {path}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

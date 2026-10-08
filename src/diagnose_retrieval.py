"""
src/diagnose_retrieval.py  -  Step 4 diagnostic. Read-only on chroma/, no cost, ids and counts only.

Raised by the first real run of retrieve.py (08/10 16:52):
  1. "Chroma returned exactly the same pool: False" for both roles.
  2. sections_rrf missed 3 of the 5 rules-strong semi-seniors (ids 22, 26, 42).

This separates the possible places a candidate can go missing, without assuming which:
  A. the metadata filter itself: col.get(where=...) is an exact lookup with no vector search, so
     comparing it with the Python rule tests the filter's meaning on its own.
  B. the vector search: col.query(..., n_results=pool size) should return the whole pool. If it
     returns fewer than A, the search (not the filter) is dropping people.
  C. for named ids: their filter flags, whether each step returned them, and their rank in every
     section query, the whole advert and the fusion.

Usage (from the candidate-rag folder, venv active):
    python src/diagnose_retrieval.py
"""

from __future__ import annotations

import sys
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

sys.path.insert(0, str(Path(__file__).resolve().parent))
from index import COLLECTION, CHROMA_DIR, load_profiles  # noqa: E402
from retrieve import (JDS, ROLES, chroma_where, python_filter, rrf,  # noqa: E402
                      section_queries, split_advert)

WATCH = {"semi-senior": ["22", "26", "42"], "credit-controller": []}


def ids_str(ids) -> str:
    ids = sorted(ids, key=int)
    return ", ".join(ids[:40]) + (" ..." if len(ids) > 40 else "") if ids else "none"


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    meta = {p["id"]: p["metadata"] for p in load_profiles()}
    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    col = client.get_collection(COLLECTION, embedding_function=embedding_functions.DefaultEmbeddingFunction())
    total = col.count()

    print("=" * 70)
    print("STEP 4 DIAGNOSTIC  (safe to paste: ids and counts only)")
    print("=" * 70)
    print(f"chromadb {chromadb.__version__}; collection count {total}")
    res = col.query(query_texts=["accountant"], n_results=total, include=["distances"])
    print(f"unfiltered query asking for all {total}: returned {len(res['ids'][0])}")

    for role, cfg in ROLES.items():
        flag = cfg["area_flag"]
        where = chroma_where(flag)
        py_pool = {pid for pid, m in meta.items() if python_filter(m, flag)}
        got = col.get(where=where, include=["metadatas"])
        get_pool = set(got["ids"])

        advert = (JDS / cfg["jd"]).read_text(encoding="utf-8")
        whole = col.query(query_texts=[advert], n_results=len(py_pool), where=where, include=["distances"])
        q_pool = set(whole["ids"][0])
        title, sections = split_advert(advert)
        queries = section_queries(title, sections)
        per = []
        for q in queries:
            r = col.query(query_texts=[q["text"]], n_results=len(py_pool), where=where, include=["distances"])
            per.append(r["ids"][0])
        fused = [pid for pid, _ in rrf(per)]

        print("")
        print(f"### {role}")
        print(f"A. filter only: Python rule {len(py_pool)}, Chroma get(where) {len(get_pool)}; "
              f"in Python not Chroma: {ids_str(py_pool - get_pool)}; in Chroma not Python: {ids_str(get_pool - py_pool)}")
        for pid in sorted((py_pool ^ get_pool), key=int)[:15]:
            m = meta[pid]
            print(f"     id {pid}: client_staff={m['client_staff']} {flag}={m[flag]} loc_uk_only={m['loc_uk_only']}")
        print(f"B. vector search asking for {len(py_pool)}: whole advert returned {len(q_pool)}; "
              f"sections returned {', '.join(str(len(x)) for x in per)}")
        print(f"   in the filter result but never returned by the whole-advert search: {ids_str(get_pool - q_pool)}")
        missing_any = set().union(*[get_pool - set(x) for x in per]) if per else set()
        print(f"   in the filter result but missing from at least one section search: {ids_str(missing_any)}")
        for pid in WATCH.get(role, []):
            m = meta[pid]
            ranks = [(x.index(pid) + 1 if pid in x else None) for x in per]
            wr = whole["ids"][0].index(pid) + 1 if pid in whole["ids"][0] else None
            fr = fused.index(pid) + 1 if pid in fused else None
            print(f"C. id {pid}: passes Python filter {pid in py_pool}, in Chroma get {pid in get_pool}; "
                  f"rank by section: " + ", ".join(f"{q['heading']} {r}" for q, r in zip(queries, ranks))
                  + f"; whole advert {wr}; fused {fr}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

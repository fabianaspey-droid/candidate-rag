"""
tools/check_query_embedding.py  -  proves the server's QueryEmbedder gives the same vectors and the same
rankings as Chroma's own embedding function, with the real model, and measures how much faster it is.

Why it exists (Part 3, 21/10): in the container at a quarter of a CPU, embedding the query was the whole
cost of a search, because Chroma pads every input to 256 tokens and ONNX Runtime starts a thread per host
core. QueryEmbedder skips the padding and uses one thread. The model masks padding out of attention and the
pooling ignores it, so the vectors should match; this script measures that rather than assuming it.

Usage (from the candidate-rag folder, venv active; sample data, read-only, no API calls, no cost):
    python tools/check_query_embedding.py
"""
import os
import statistics
import sys
import time
from pathlib import Path

os.environ.pop("CANDIDATE_RAG_REAL", None)
os.environ["CANDIDATE_RAG_SAMPLE"] = "1"
os.environ["CANDIDATE_RAG_READONLY"] = "1"
PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))

import numpy as np  # noqa: E402
from chromadb.utils.embedding_functions import ONNXMiniLM_L6_V2  # noqa: E402

from index import load_profiles  # noqa: E402
from paths import DATA, JDS  # noqa: E402
from retrieve import ROLES  # noqa: E402
from search import QueryEmbedder, build_where, close_temp_copies, hybrid_rank, open_collection, python_filter  # noqa: E402

QUERIES = ["credit controller", "credit controller chasing aged debt", "AAT qualified semi senior accountant",
           "ACCA student accounts preparation VAT personal tax", "CICM", "sales ledger and cash allocation",
           "first finance role, customer service background", "audit senior", "bookkeeper", "x"]
MIN_COSINE = 0.99999
MAX_ABS_DIFF = 1e-4
results = []


def record(name: str, ok: bool, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail[:220]}")


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    chroma_ef, ours = ONNXMiniLM_L6_V2(), QueryEmbedder()
    profiles = load_profiles(DATA / "profiles.jsonl")
    texts = {p["id"]: p["text"] for p in profiles}
    meta = {p["id"]: p["metadata"] for p in profiles}
    adverts = [f.read_text(encoding="utf-8") for f in sorted(JDS.glob("*.txt"))]
    inputs = QUERIES + [p["text"] for p in profiles] + adverts  # short, typical and longer than 256 tokens

    a = np.array(chroma_ef(inputs), dtype=np.float64)
    b = np.array([ours(t) for t in inputs], dtype=np.float64)
    cos = np.sum(a * b, axis=1) / (np.linalg.norm(a, axis=1) * np.linalg.norm(b, axis=1))
    diff = np.max(np.abs(a - b))
    record(f"same vectors as Chroma for {len(inputs)} inputs (queries, every profile, both adverts)",
           cos.min() >= MIN_COSINE and diff <= MAX_ABS_DIFF,
           f"lowest cosine similarity {cos.min():.8f} (need {MIN_COSINE}), largest difference in any "
           f"dimension {diff:.2e} (need under {MAX_ABS_DIFF:.0e})")

    col = open_collection(read_only_copy=True)
    try:
        same_semantic, same_hybrid, total = 0, 0, 0
        first_diff = ""
        for role, cfg in ROLES.items():
            where, _ = build_where(cfg["area_flag"], {})
            pool = {pid for pid, m in meta.items() if python_filter(m, cfg["area_flag"])}
            for q in QUERIES:
                total += 1
                by_text = col.query(query_texts=[q], n_results=len(pool), where=where, include=["distances"])["ids"][0]
                by_vec = col.query(query_embeddings=[ours(q)], n_results=len(pool), where=where, include=["distances"])["ids"][0]
                same_semantic += by_text == by_vec
                h1 = [pid for pid, _ in hybrid_rank(col, q, where, texts, pool)]
                h2 = [pid for pid, _ in hybrid_rank(col, q, where, texts, pool, embed=ours)]
                same_hybrid += h1 == h2
                if (by_text != by_vec or h1 != h2) and not first_diff:
                    first_diff = f"; first difference: {role} / '{q}'"
        record("identical semantic ranking of the whole pool, every query and role", same_semantic == total,
               f"{same_semantic} of {total} identical{first_diff}")
        record("identical hybrid ranking (what search_candidates returns), every query and role", same_hybrid == total,
               f"{same_hybrid} of {total} identical")
    finally:
        del col
        close_temp_copies()

    def per_call(fn, n=20):
        fn(QUERIES[1])
        times = []
        for i in range(n):
            t0 = time.perf_counter()
            fn(QUERIES[i % len(QUERIES)])
            times.append(time.perf_counter() - t0)
        return statistics.median(times)
    slow, fast = per_call(lambda q: chroma_ef([q])), per_call(ours)
    print(f"INFO  query embedding on this machine, median of 20: Chroma {slow * 1000:.0f} ms, "
          f"QueryEmbedder {fast * 1000:.1f} ms ({slow / fast:.0f}x faster)")

    print("=" * 70)
    print(f"{sum(results)} of {len(results)} query-embedding checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(main())

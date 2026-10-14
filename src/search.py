"""
src/search.py  -  the search functions, shared by retrieve.py, agent.py and the MCP server.

Moved out of retrieve.py and agent.py on 13/10/2026 without changing their behaviour (checked by
comparing retrieval output byte for byte before and after). Nothing here writes anything.

  open_collection()   the Chroma collection for the current mode (real or --sample), with a clear
                      error if the index is missing or empty
  chroma_where / python_filter / build_where   the hard filters (client staff out; role's area or
                      "United Kingdom" only) plus optional qualification flags
  embed_rank          semantic ranking (what the Step 7 agent uses)
  bm25, rrf           keyword ranking and reciprocal rank fusion
  hybrid_rank         semantic + BM25 fused, for a free-text query (new; for the MCP server)
"""

from __future__ import annotations

import math
import re
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import CHROMA_DIR  # noqa: E402

COLLECTION = "candidates"
RRF_K = 60
SNIPPET_CHARS = 300
FILTER_FLAGS = ["aat_qualified", "aca_acca_held", "aca_acca_in_progress", "cicm", "has_any_qual"]

STOPWORDS = set("""a an and are as at be been but by can for from has have i if in into is it its
of on or our so that the their them they this to up we what who will with you your youll yourll
re ll ve s t""".split())
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


def build_where(area_flag: str, filters: dict | None = None) -> tuple[dict, dict]:
    """Hard filters plus any valid qualification flags. Returns (where clause, flags actually used).
    Unknown keys and non-boolean values are ignored, so a caller can narrow but never widen."""
    clauses = [chroma_where(area_flag)]
    used = {}
    for k, v in (filters or {}).items():
        if k in FILTER_FLAGS and isinstance(v, bool):
            clauses.append({k: {"$eq": v}})
            used[k] = v
    return (clauses[0] if len(clauses) == 1 else {"$and": clauses}), used


def hybrid_rank(col, query: str, where: dict, texts: dict[str, str], pool: set[str]) -> list[tuple[str, float]]:
    """Semantic ranking over the whole filtered pool, fused with BM25 over the same pool (one vote
    each, RRF). The same fusion retrieve.py uses for hybrid_rrf, applied to a free-text query."""
    if not pool:
        return []
    semantic = [pid for pid, _ in embed_rank(col, query, where, len(pool))]
    keyword = [pid for pid, _ in bm25(query, texts, pool)]
    return rrf([semantic, keyword])


def close_temp_copies() -> list[str]:
    """Release Chroma's files, then delete the temporary index copies. Returns any folders that could not
    be removed. On Windows a folder whose files are still open cannot be deleted (seen 14/10 with a test
    index), so Chroma's cached systems are stopped first."""
    import gc
    try:
        from chromadb.api.shared_system_client import SharedSystemClient
        for system in list(SharedSystemClient._identifier_to_system.values()):
            try:
                system.stop()
            except Exception:
                pass
        SharedSystemClient.clear_system_cache()
    except Exception:
        pass
    gc.collect()
    left = []
    for tmp in list(_TEMP_COPIES):
        tmp.cleanup()  # ignore_cleanup_errors=True: never raises
        if Path(tmp.name).exists():
            left.append(tmp.name)
        else:
            _TEMP_COPIES.remove(tmp)
    return left


class IndexNotReady(RuntimeError):
    pass


_TEMP_COPIES: list = []  # kept alive for the life of the process, removed at exit


def open_collection(read_only_copy: bool = False):
    """The candidates collection for the current mode. Raises IndexNotReady with what to run first.

    read_only_copy=True copies the index folder to a temporary folder and opens the copy, so the real
    index is never opened. Measured 14/10: chromadb 1.5.9 changes chroma.sqlite3 and HNSW files on every
    PersistentClient open, even an open followed only by count(). The MCP server uses this.
    """
    import atexit
    import shutil
    import tempfile

    import chromadb
    from chromadb.utils import embedding_functions
    run = "python src/index.py" + (" --sample" if "sample" in CHROMA_DIR.parts else "")
    if not CHROMA_DIR.exists() or not (CHROMA_DIR / "chroma.sqlite3").exists():
        raise IndexNotReady(f"No index at {CHROMA_DIR}. Run: {run}")
    path = CHROMA_DIR
    if read_only_copy:
        tmp = tempfile.TemporaryDirectory(prefix="candidate_rag_index_", ignore_cleanup_errors=True)
        _TEMP_COPIES.append(tmp)
        atexit.register(tmp.cleanup)
        path = Path(tmp.name) / "chroma"
        shutil.copytree(CHROMA_DIR, path)
    client = chromadb.PersistentClient(path=str(path))
    try:
        col = client.get_collection(COLLECTION, embedding_function=embedding_functions.DefaultEmbeddingFunction())
    except Exception as e:
        raise IndexNotReady(f"No '{COLLECTION}' collection in {CHROMA_DIR} ({type(e).__name__}). Run: {run}")
    if col.count() == 0:
        raise IndexNotReady(f"The index at {CHROMA_DIR} is empty. Run: {run}")
    return col

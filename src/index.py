"""
src/index.py  -  Step 3 of candidate-rag: embed every profile and store it in ChromaDB.

Reads data/profiles.jsonl (from Step 2) and upserts every record into a persistent Chroma
database in chroma/, collection "candidates", cosine distance, using Chroma's default embedding
function (all-MiniLM-L6-v2, run locally through ONNX: no API key, no cost). The first run
downloads the model (about 80 MB) to your user folder's .cache\\chroma.

Design decisions:
  * upsert keyed on sourcing-core's person_id, so running twice updates rather than duplicates.
  * eval_* keys (sourcing-core's verdicts) are NOT stored in Chroma. They stay in
    data/profiles.jsonl for Step 6 only, so retrieval physically cannot filter or rank on them.
  * Chroma accepts only str, int, float and bool metadata: None is dropped, lists are joined with "|".

Checks printed (counts and job titles only, safe to paste):
  * collection count before and after, ids in Chroma == ids in profiles.jsonl, no eval_* keys stored
  * the collection's distance space read back from Chroma
  * real word-piece counts from the model's own tokenizer, for every profile and every jds/*.txt
  * two sanity queries, top 5 each

Usage (from the candidate-rag folder, venv active):
    python src/index.py
"""

from __future__ import annotations

import json
import statistics
import sys
from pathlib import Path

import chromadb
from chromadb.utils import embedding_functions

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import CHROMA_DIR, DATA, JDS as JDS_DIR, PROJECT  # noqa: E402,F401

PROFILES = DATA / "profiles.jsonl"
COLLECTION = "candidates"
MODEL_WINDOW = 256  # word pieces the model reads, including the two special tokens
# HNSW is an approximate search. chromadb 1.5.9 defaults ef_search to 100; on 08/10 a filtered query
# asking for all 138 people in the pool returned 137 (id 137 missing, every time). Setting ef_search
# above the collection size makes the search visit the whole graph at this scale. At Websters scale
# (12,000) this becomes a recall-versus-speed setting to measure, not a constant.
EF_SEARCH = 500

SANITY_QUERIES = [
    ("credit controller chasing aged debt", "credit_controller"),
    ("semi-senior accountant studying ACCA", "semi_senior"),
]


def load_profiles(path: Path = PROFILES) -> list[dict]:
    with path.open(encoding="utf-8") as f:
        return [json.loads(line) for line in f if line.strip()]


def chroma_metadata(meta: dict) -> dict:
    """Make metadata Chroma-safe and strip evaluation-only keys."""
    out = {}
    for k, v in meta.items():
        if k.startswith("eval_") or v is None:
            continue
        if isinstance(v, (list, tuple, set)):
            v = "|".join(str(x) for x in v)
        elif not isinstance(v, (str, int, float, bool)):
            v = str(v)
        out[k] = v
    return out


def get_collection(client, embedding_function):
    col = client.get_or_create_collection(
        name=COLLECTION,
        embedding_function=embedding_function,
        configuration={"hnsw": {"space": "cosine", "ef_search": EF_SEARCH}},
    )
    if hnsw_setting(col, "ef_search") != EF_SEARCH:  # collection created before EF_SEARCH existed
        col.modify(configuration={"hnsw": {"ef_search": EF_SEARCH}})
    return col


def hnsw_setting(collection, key: str):
    try:
        cfg = collection.configuration
        hnsw = cfg.get("hnsw") if isinstance(cfg, dict) else getattr(cfg, "hnsw", None)
        return hnsw.get(key) if isinstance(hnsw, dict) else getattr(hnsw, key, None)
    except Exception:
        return None


def upsert_profiles(collection, profiles: list[dict], batch: int = 64) -> None:
    for i in range(0, len(profiles), batch):
        chunk = profiles[i:i + batch]
        collection.upsert(
            ids=[p["id"] for p in chunk],
            documents=[p["text"] for p in chunk],
            metadatas=[chroma_metadata(p["metadata"]) for p in chunk],
        )


def distance_space(collection) -> str:
    try:
        cfg = collection.configuration
        hnsw = cfg.get("hnsw") if isinstance(cfg, dict) else getattr(cfg, "hnsw", None)
        if isinstance(hnsw, dict):
            return str(hnsw.get("space"))
        return str(getattr(hnsw, "space", "unknown"))
    except Exception as e:  # report rather than fail the run
        return f"unknown ({type(e).__name__})"


def word_piece_counts(texts: list[str]) -> list[int] | None:
    """Count word pieces with the model's own tokenizer, without truncation."""
    try:
        from tokenizers import Tokenizer
        from chromadb.utils.embedding_functions.onnx_mini_lm_l6_v2 import ONNXMiniLM_L6_V2
        path = Path(ONNXMiniLM_L6_V2.DOWNLOAD_PATH) / ONNXMiniLM_L6_V2.EXTRACTED_FOLDER_NAME / "tokenizer.json"
        tok = Tokenizer.from_file(str(path))  # a fresh copy, so the embedder's own settings are untouched
        tok.no_truncation()
        tok.no_padding()
        return [len(tok.encode(t).ids) for t in texts]
    except Exception as e:
        print(f"  (word-piece count unavailable: {type(e).__name__}: {e})")
        return None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if not PROFILES.is_file():
        print(f"ERROR: {PROFILES} not found. Run src/ingest.py first.")
        return 2

    profiles = load_profiles()
    labels = {p["id"]: p["metadata"] for p in profiles}

    client = chromadb.PersistentClient(path=str(CHROMA_DIR))
    ef = embedding_functions.DefaultEmbeddingFunction()
    col = get_collection(client, ef)
    before = col.count()
    upsert_profiles(col, profiles)
    after = col.count()

    stored = col.get(include=["metadatas"])
    stored_ids = set(stored["ids"])
    profile_ids = {p["id"] for p in profiles}
    eval_keys = sorted({k for m in stored["metadatas"] for k in m if k.startswith("eval_")})

    print("=" * 70)
    print("STEP 3 CHECK  (safe to paste: counts and job titles only)")
    print("=" * 70)
    print(f"profiles in {PROFILES.relative_to(PROJECT)}: {len(profiles)}")
    print(f"collection '{COLLECTION}' count before upsert: {before}, after: {after}")
    print(f"ids in Chroma == ids in profiles: {stored_ids == profile_ids} "
          f"(missing {len(profile_ids - stored_ids)}, extra {len(stored_ids - profile_ids)})")
    print(f"eval_* keys stored in Chroma (must be none): {eval_keys or 'none'}")
    print(f"distance space: {distance_space(col)}; ef_search: {hnsw_setting(col, 'ef_search')}")
    emb = col.get(ids=[profiles[0]["id"]], include=["embeddings"])["embeddings"]
    print(f"embedding dimensions: {len(emb[0]) if emb is not None and len(emb) else 'unknown'}")

    counts = word_piece_counts([p["text"] for p in profiles])
    if counts:
        over = sum(1 for c in counts if c > MODEL_WINDOW)
        print(f"profile word pieces: min {min(counts)}, median {statistics.median(counts)}, "
              f"max {max(counts)}; over the {MODEL_WINDOW} window: {over}")
    jd_files = sorted(JDS_DIR.glob("*.txt"))
    jd_counts = word_piece_counts([f.read_text(encoding="utf-8") for f in jd_files]) if jd_files else None
    if jd_counts:
        for f, c in zip(jd_files, jd_counts):
            print(f"job description {f.name}: {c} word pieces"
                  + (f" -> the model reads only the first {MODEL_WINDOW}" if c > MODEL_WINDOW else ""))

    for query, role_key in SANITY_QUERIES:
        res = col.query(query_texts=[query], n_results=5, include=["metadatas", "distances"])
        print("")
        print(f'query: "{query}"  (top 5; eval label shown for checking only, not used in the query)')
        for rank, (pid, meta, dist) in enumerate(
                zip(res["ids"][0], res["metadatas"][0], res["distances"][0]), 1):
            label = labels[pid].get(f"eval_label_{role_key}", "?")
            print(f"  {rank}. id {pid:>4}  distance {dist:.3f}  {meta.get('current_title') or '(no current role)'}"
                  f"  | quals: {meta.get('qual_codes') or 'none'}  | rules: {label}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

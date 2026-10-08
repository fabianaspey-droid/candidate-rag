"""The double-run upsert check from Step 3: indexing the same profiles twice must not duplicate them.
Uses an in-memory Chroma and a tiny stand-in embedder, so no model download and no files written."""
import sys
from pathlib import Path

import chromadb
import numpy as np
from chromadb.api.types import EmbeddingFunction

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / "src"))
from index import chroma_metadata, upsert_profiles  # noqa: E402


class TinyEmbedder(EmbeddingFunction):
    def __init__(self):
        pass

    def __call__(self, input):
        return [np.array([len(t) % 7 + 1.0, t.count(" ") + 1.0, 1.0], dtype=np.float32) for t in input]

    @staticmethod
    def name():
        return "tiny-test-embedder"

    def get_config(self):
        return {}

    @staticmethod
    def build_from_config(config):
        return TinyEmbedder()


PROFILES = [
    {"id": str(i), "text": f"Credit Controller number {i}",
     "metadata": {"person_id": i, "client_staff": False, "tags": ["a", "b"], "missing": None,
                  "eval_label_credit_controller": "strong"}}
    for i in range(1, 6)
]


def test_double_upsert_does_not_duplicate():
    client = chromadb.EphemeralClient()
    col = client.get_or_create_collection("upsert_test", embedding_function=TinyEmbedder())
    upsert_profiles(col, PROFILES)
    upsert_profiles(col, PROFILES)
    assert col.count() == len(PROFILES)


def test_metadata_is_chroma_safe_and_has_no_eval_keys():
    m = chroma_metadata(PROFILES[0]["metadata"])
    assert m["tags"] == "a|b" and "missing" not in m
    assert not any(k.startswith("eval_") for k in m)

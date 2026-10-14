"""
tests/conftest.py  -  runs before any test module is imported.

Pins every test to the INVENTED SAMPLE, so a test can never read the real data in data/, whatever the
shell environment says; stops paths.py creating folders; and points the index at a throwaway folder
that tests/test_mcp_server.py fills with a stand-in embedder (no model download, same result every run).
"""
import os
import tempfile

os.environ.pop("CANDIDATE_RAG_REAL", None)
os.environ["CANDIDATE_RAG_SAMPLE"] = "1"
os.environ["CANDIDATE_RAG_READONLY"] = "1"
TEST_INDEX_DIR = tempfile.mkdtemp(prefix="candidate_rag_test_index_")
os.environ["CANDIDATE_RAG_CHROMA_DIR"] = TEST_INDEX_DIR

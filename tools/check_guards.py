"""
tools/check_guards.py  -  Step 4 check: every guard, against the REAL server over stdio, on the sample.

Launches `python src/mcp_server.py` as a subprocess (exactly how the Claude desktop app runs it), sends
good and bad calls, and prints PASS or FAIL for each. Also launches the server against a missing index
and an empty index (temporary folders, via CANDIDATE_RAG_CHROMA_DIR), and fingerprints sample/ before and
after to prove nothing was written. No API calls, no cost. Output holds only invented sample data.

Usage (from the candidate-rag folder, venv active):
    python tools/check_guards.py
"""
import asyncio
import hashlib
import os
import subprocess
import sys
import tempfile
from pathlib import Path

from mcp import Client, StdioServerParameters

PROJECT = Path(__file__).resolve().parents[1]
SERVER = [str(PROJECT / "src" / "mcp_server.py")]
results = []


def fingerprint(folder: Path) -> dict[str, str]:
    out = {}
    for p in sorted(folder.rglob("*")):
        if p.is_file() and "out" not in p.relative_to(folder).parts:
            out[str(p.relative_to(folder))] = hashlib.sha256(p.read_bytes()).hexdigest()
    return out


def record(name: str, ok: bool, detail: str) -> None:
    results.append(ok)
    print(f"{'PASS' if ok else 'FAIL'}  {name}\n      {detail[:230]}")


def text_of(r) -> str:
    return " ".join(getattr(c, "text", "") for c in r.content).replace("\n", " ")


async def expect_error(c, name, tool, args, must_contain):
    r = await c.call_tool(tool, args)
    msg = text_of(r)
    record(name, bool(r.is_error) and must_contain.lower() in msg.lower(), msg)


def params(extra_env: dict | None = None) -> StdioServerParameters:
    return StdioServerParameters(command=sys.executable, args=SERVER, cwd=str(PROJECT), env=extra_env or {})


def temp_copies() -> set[str]:
    return {str(p) for p in Path(tempfile.gettempdir()).glob("candidate_rag_index_*")}


async def main() -> int:
    copies_before = temp_copies()
    before = fingerprint(PROJECT / "sample")
    async with Client(params()) as c:
        r = await c.call_tool("search_candidates", {"role": "semi-senior", "query": "AAT qualified semi senior accountant", "n_results": 5})
        sc = r.structured_content or {}
        record("normal search returns ranked results", not r.is_error and len(sc.get("results", [])) == 5,
               f"pool {sc.get('pool_size')}, top: " + ", ".join(f"{h['id']} {h['title']}" for h in sc.get("results", [])[:3])
               if not r.is_error else text_of(r))
        record("results are snippets, not full profiles", not r.is_error and all(len(h["snippet"]) <= 300 for h in sc.get("results", [])),
               f"longest snippet {max((len(h['snippet']) for h in sc.get('results', [])), default=0)} characters")
        await expect_error(c, "21 results is rejected", "search_candidates", {"role": "semi-senior", "query": "x", "n_results": 21}, "less than or equal to 20")
        await expect_error(c, "0 results is rejected", "search_candidates", {"role": "semi-senior", "query": "x", "n_results": 0}, "greater than or equal to 1")
        await expect_error(c, "unknown role is rejected", "search_candidates", {"role": "astronaut", "query": "x"}, "Unknown role")
        await expect_error(c, "unknown filter is rejected", "search_candidates", {"role": "semi-senior", "query": "x", "filters": {"likes_cats": True}}, "Extra inputs are not permitted")
        await expect_error(c, "non-boolean filter value is rejected", "search_candidates", {"role": "semi-senior", "query": "x", "filters": {"cicm": "yes"}}, "valid boolean")
        await expect_error(c, "empty query is rejected", "search_candidates", {"role": "semi-senior", "query": "   "}, "query is empty")
        await expect_error(c, "over-long query is rejected", "search_candidates", {"role": "semi-senior", "query": "x" * 501}, "at most 500")
        await expect_error(c, "made-up candidate id", "get_profile", {"candidate_id": "99999"}, "No candidate with id")
        await expect_error(c, "client staff are invisible (same error as made-up id)", "get_profile", {"candidate_id": "8"}, "No candidate with id")
        r = await c.call_tool("get_profile", {"candidate_id": 10})
        record("a numeric id is accepted the same as text", not r.is_error and (r.structured_content or {}).get("id") == "10", text_of(r)[:120])
        r = await c.call_tool("get_profile", {"candidate_id": "10"})
        prof = (r.structured_content or {}).get("profile", "")
        record("real candidate id returns the full profile", not r.is_error and prof.startswith("Current role:"),
               prof[:120] if prof else text_of(r))
        try:
            await c.read_resource("jd://astronaut")
            record("unknown role advert is an error", False, "returned content")
        except Exception as e:
            record("unknown role advert is an error", "No job advert" in str(e), str(e))
        try:
            await c.get_prompt("shortlist_for_role", {"role": "semi-senior", "how_many": "50"})
            record("prompt rejects how_many over 20", False, "returned a prompt")
        except Exception as e:
            record("prompt rejects how_many over 20", "between 1 and 20" in str(e), str(e))
        try:
            await c.get_prompt("shortlist_for_role", {"role": "astronaut"})
            record("prompt rejects an unknown role", False, "returned a prompt")
        except Exception as e:
            record("prompt rejects an unknown role", "Unknown role" in str(e), str(e))

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as missing:
        async with Client(params({"CANDIDATE_RAG_CHROMA_DIR": str(Path(missing) / "nothing_here")})) as c:
            await expect_error(c, "missing index says what to run", "search_candidates", {"role": "semi-senior", "query": "x"}, "Run: python src/index.py")

    with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as empty:
        # made in a separate process, so this script never holds the index files open (Windows locks them)
        subprocess.run([sys.executable, "-c", "import chromadb, sys; "
                        "chromadb.PersistentClient(path=sys.argv[1]).get_or_create_collection('candidates')", empty],
                       check=True, capture_output=True)
        async with Client(params({"CANDIDATE_RAG_CHROMA_DIR": empty})) as c:
            await expect_error(c, "empty index says what to run", "search_candidates", {"role": "semi-senior", "query": "x"}, "is empty")

    after = fingerprint(PROJECT / "sample")
    changed = [k for k in set(before) | set(after) if before.get(k) != after.get(k)]
    record("read-only: sample folder and index unchanged by every call above", not changed,
           f"{len(before)} files fingerprinted; changed: {changed or 'none'}")
    await asyncio.sleep(1)
    new_copies = temp_copies() - copies_before
    record("server deletes its temporary index copies on exit", not new_copies,
           f"left behind in {tempfile.gettempdir()}: {sorted(new_copies) or 'none'}")
    print("=" * 70)
    print(f"{sum(results)} of {len(results)} checks passed")
    return 0 if all(results) else 1


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))

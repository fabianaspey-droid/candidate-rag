"""
src/paths.py  -  where every script reads and writes, for real data or the invented sample.

Real mode (default): inputs and outputs in data/, index in chroma/, job adverts in jds/. All three are
git-ignored, so nothing real is ever published.

Sample mode (add --sample to any command, or set CANDIDATE_RAG_SAMPLE=1): inputs from sample/
(invented candidates, invented adverts and an invented answer key, all committed), outputs to
sample/out/ and the index to sample/chroma/ (both git-ignored). Lets anyone run the pipeline without
access to the private source data.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]

SAMPLE = os.environ.get("CANDIDATE_RAG_SAMPLE") == "1" or "--sample" in sys.argv
if "--sample" in sys.argv:
    sys.argv.remove("--sample")  # so each script's own argument parser never sees it

if SAMPLE:
    DATA = PROJECT / "sample"            # inputs: profiles.jsonl, labels.csv
    OUT = PROJECT / "sample" / "out"     # outputs: retrieval_*, rerank_*, agent_*, RESULTS.md
    CHROMA_DIR = PROJECT / "sample" / "chroma"
    JDS = PROJECT / "sample" / "jds"
    RESULTS = OUT / "RESULTS.md"
else:
    DATA = PROJECT / "data"
    OUT = PROJECT / "data"
    CHROMA_DIR = PROJECT / "chroma"
    JDS = PROJECT / "jds"
    RESULTS = PROJECT / "RESULTS.md"

# For guard checks only: point the index somewhere else (e.g. an empty folder) without touching the real one.
if os.environ.get("CANDIDATE_RAG_CHROMA_DIR"):
    CHROMA_DIR = Path(os.environ["CANDIDATE_RAG_CHROMA_DIR"])

# The MCP server sets CANDIDATE_RAG_READONLY=1 so that importing this module creates nothing.
if os.environ.get("CANDIDATE_RAG_READONLY") != "1":
    OUT.mkdir(parents=True, exist_ok=True)

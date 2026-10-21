"""
src/mcp_server.py  -  the candidate-rag search as an MCP server (Model Context Protocol).

Any MCP-compatible assistant (the Claude desktop app, Claude Code, MCP Inspector) can connect to it and
search the candidate pool itself. Built on the official MCP Python SDK, mcp 2.3.0
(`from mcp.server.mcpserver import MCPServer`). Two transports:
  * stdio (the default), for a client that launches the server itself, such as the desktop app config;
  * Streamable HTTP (`--http`, Part 3), for a remote server that clients reach by URL at /mcp.

Exposes (agreed 14/10, see BUILD_LOG.md Part 2, Step 2):
  tool      list_roles          the roles the pool can be searched against
  tool      search_candidates   hybrid search (semantic + BM25) for one role, inside its hard filters
  tool      get_profile         one candidate's full profile
  resource  jd://{role}         the full job advert for a role
  prompt    shortlist_for_role  read the advert, search from several angles, shortlist with evidence

Rules:
  * Sample data by default. Real data ONLY when CANDIDATE_RAG_REAL=1 is set for this process.
  * Read-only. No tool writes anything; importing paths.py is told not to create its output folder; the
    index is copied to a temporary folder and the copy opened, because Chroma writes to its own files on
    every open (measured 14/10).
  * The client's own staff are never returned by any tool.
  * Anticipated failures raise ToolError, so the assistant reads a clear message; never an empty result
    that looks like "no matches".
  * Logs go to stderr only. On the stdio transport, stdout carries the protocol messages; anything
    else printed there corrupts them.
  * HTTP mode is stateless with plain JSON replies (no session to lose when a load balancer moves a client
    between copies), checks the Host and Origin headers itself (binding to 0.0.0.0 switches off the SDK's
    own check), and only starts listening once the index is open and the model has embedded a query.

Run (from the candidate-rag folder, venv active):
    python src/mcp_server.py                       # sample data; waits for a client on stdin/stdout
    $env:CANDIDATE_RAG_REAL="1"; python src/mcp_server.py   # real data (PowerShell), local only
    python src/mcp_server.py --http                # sample data over HTTP at http://127.0.0.1:8000/mcp

HTTP settings (environment variables, all optional):
    MCP_TRANSPORT=http      same as --http
    MCP_HOST                address to listen on (default 127.0.0.1; 0.0.0.0 inside a container)
    MCP_PORT                port (default 8000)
    MCP_ALLOWED_HOSTS       comma-separated public hostnames clients use, e.g. the AWS service's hostname;
                            localhost and 127.0.0.1 are always allowed
    MCP_ALLOWED_ORIGINS     comma-separated extra browser origins (claude.ai, claude.com and local ones are allowed)
"""

from __future__ import annotations

import logging
import os
import sys
import threading
import time
from functools import lru_cache
from pathlib import Path
from typing import Annotated, TypedDict

# ---- choose the data BEFORE paths.py is imported, and keep paths.py from creating folders
REAL = os.environ.get("CANDIDATE_RAG_REAL") == "1"
if REAL:
    os.environ.pop("CANDIDATE_RAG_SAMPLE", None)
else:
    os.environ["CANDIDATE_RAG_SAMPLE"] = "1"
os.environ["CANDIDATE_RAG_READONLY"] = "1"

logging.basicConfig(stream=sys.stderr, level=logging.INFO,
                    format="%(asctime)s %(levelname)s %(name)s: %(message)s")  # name: ours is candidate-rag
log = logging.getLogger("candidate-rag")
# The SDK's HTTP transport logs "Terminating session: None" at INFO on every stateless request (seen 20/10);
# that would be one useless CloudWatch line per call. Its warnings and errors still show.
logging.getLogger("mcp.server.streamable_http").setLevel(logging.WARNING)

sys.path.insert(0, str(Path(__file__).resolve().parent))
from pydantic import BaseModel, ConfigDict, Field, StrictBool  # noqa: E402

from mcp import MCPError  # noqa: E402
from mcp.server.mcpserver import MCPServer  # noqa: E402
from mcp.server.mcpserver.exceptions import ResourceNotFoundError, ToolError  # noqa: E402
from mcp.server.transport_security import TransportSecuritySettings  # noqa: E402
from mcp.types import ToolAnnotations  # noqa: E402
from starlette.requests import Request  # noqa: E402
from starlette.responses import JSONResponse  # noqa: E402

from index import load_profiles  # noqa: E402
from paths import DATA, JDS, SAMPLE  # noqa: E402
from retrieve import ROLES  # noqa: E402
from search import (SNIPPET_CHARS, IndexNotReady, QueryEmbedder, build_where, hybrid_rank,  # noqa: E402
                    open_collection, python_filter)

MAX_RESULTS = 20
MAX_QUERY_CHARS = 500
INVALID_PARAMS = -32602  # JSON-RPC "invalid params"; a plain exception from a prompt reaches the client only as
                         # "Internal server error" (measured 14/10), so prompts raise MCPError with this code
MODE = "sample" if SAMPLE else "real"
ROLE_SUMMARIES = {
    "credit-controller": ("Credit controller in an accountancy practice: chasing client debt, resolving billing "
                          "queries with partners, aged debt reporting. Credit control experience, or someone "
                          "ready for a first finance role."),
    "semi-senior": ("Semi-senior accountant in practice: accounts preparation, VAT, personal tax and "
                    "bookkeeping. AAT qualified, or studying towards ACA or ACCA."),
}
MCP_PATH = "/mcp"
DEFAULT_PORT = 8000
LOCAL_HOSTS = ["127.0.0.1:*", "localhost:*", "[::1]:*"]
ALLOWED_ORIGINS = ["https://claude.ai", "https://claude.com",
                   "http://127.0.0.1:*", "http://localhost:*", "http://[::1]:*"]  # local ones: the Inspector's web UI
# One search at a time. The SDK runs each tool call on its own worker thread, and every search in progress
# holds its own working memory (embedding and Chroma query). Measured 20/10 in the container at 0.25 vCPU:
# idle 109 MB, ten searches at once about 1.1 GB, which crashed the 1 GB container (OOM, exit 137). One at a
# time, memory stays flat (about 215 MB after 30 searches). On a quarter of a CPU, parallel searches can't
# finish sooner anyway, so queuing them costs no throughput. Also makes the first index open thread-safe.
_SEARCH_LOCK = threading.Lock()
READ_ONLY = ToolAnnotations(read_only_hint=True, destructive_hint=False, idempotent_hint=True,
                            open_world_hint=False)

mcp = MCPServer(
    "candidate-rag",
    instructions=(
        "Search a recruitment candidate pool. Start with list_roles, read the role's advert at jd://<role>, "
        "then call search_candidates several times from different angles, and get_profile before recommending "
        "anyone. Quote evidence only from get_profile text. All tools are read-only."
    ),
)


# ---------------------------------------------------------------------------- shapes
class Filters(BaseModel):
    """Optional qualification filters. Each one, if given, must be true or false."""
    model_config = ConfigDict(extra="forbid")
    aat_qualified: StrictBool | None = Field(None, description="AAT qualified (held or likely held)")
    aca_acca_held: StrictBool | None = Field(None, description="ACA or ACCA held")
    aca_acca_in_progress: StrictBool | None = Field(None, description="ACA or ACCA studying, part-qualified or finalist")
    cicm: StrictBool | None = Field(None, description="Any CICM (credit management) qualification")
    has_any_qual: StrictBool | None = Field(None, description="At least one qualification stated")


class Role(TypedDict):
    role: str
    title: str
    summary: str
    jd_uri: str


class RoleList(TypedDict):
    mode: str
    roles: list[Role]


class Hit(TypedDict):
    rank: int
    id: str
    title: str
    employer: str
    location: str
    qualifications: list[str]
    snippet: str
    score: float


class SearchResult(TypedDict):
    role: str
    query: str
    filters_applied: dict[str, bool]
    pool_size: int
    results: list[Hit]


class Profile(TypedDict):
    id: str
    title: str
    employer: str
    location: str
    qualifications: list[str]
    profile: str


# ---------------------------------------------------------------------------- data (loaded once, read-only)
@lru_cache(maxsize=1)
def _profiles() -> tuple[dict[str, str], dict[str, dict]]:
    path = DATA / "profiles.jsonl"
    if not path.is_file():
        run = "python tools/make_sample.py" if SAMPLE else "python src/ingest.py"
        raise ToolError(f"No profiles at {path}. Run: {run}")
    rows = load_profiles(path)
    if not rows:
        raise ToolError(f"{path} is empty.")
    return {r["id"]: r["text"] for r in rows}, {r["id"]: r["metadata"] for r in rows}


@lru_cache(maxsize=1)
def _collection():
    try:
        return open_collection(read_only_copy=True)  # the real index is never opened (see search.py)
    except IndexNotReady as e:
        raise ToolError(str(e)) from e


@lru_cache(maxsize=1)
def _query_embedder() -> QueryEmbedder:
    try:
        return QueryEmbedder()  # same vectors as Chroma's, without padding to 256 tokens (see search.py)
    except IndexNotReady as e:
        raise ToolError(str(e)) from e


def _role(role: str) -> dict:
    if role not in ROLES:
        raise ToolError(f"Unknown role '{role}'. Known roles: {', '.join(sorted(ROLES))}. Call list_roles.")
    return ROLES[role]


def _title(role: str) -> str:
    try:
        return (JDS / ROLES[role]["jd"]).read_text(encoding="utf-8").splitlines()[0].split("|")[0].strip()
    except (OSError, IndexError):
        return role


def _quals(meta: dict) -> list[str]:
    return [q for q in (meta.get("qual_codes") or "").split("|") if q]


# ---------------------------------------------------------------------------- tools
@mcp.tool(annotations=READ_ONLY)
def list_roles() -> RoleList:
    """List the roles this candidate pool can be searched against. Returns each role's id, title, a one-line
    summary and the URI of its full job description (jd://<role>). Call this first if you don't know the
    role ids: search_candidates and the shortlist_for_role prompt both need one."""
    return {"mode": MODE, "roles": [
        {"role": r, "title": _title(r), "summary": ROLE_SUMMARIES.get(r, ""), "jd_uri": f"jd://{r}"}
        for r in ROLES]}


@mcp.tool(annotations=READ_ONLY)
def search_candidates(
    role: Annotated[str, Field(description="Role id from list_roles, e.g. 'semi-senior'")],
    query: Annotated[str, Field(max_length=MAX_QUERY_CHARS, description="What to look for, in plain words (up to 500 characters)")],
    n_results: Annotated[int, Field(ge=1, le=MAX_RESULTS, description="How many to return, 1 to 20")] = 10,
    filters: Filters | None = None,
) -> SearchResult:
    """Search the candidate pool for one role, by meaning and by keyword together (hybrid search). The role's
    hard filters are always applied: only candidates in that role's location area, or whose location says
    just 'United Kingdom', and never the client's own staff. You can narrow further with qualification
    filters. Returns up to n_results candidates (1 to 20, default 10), best first, each with id, current
    title, employer, location, qualification codes, the first 300 characters of their profile and a relevance
    score that can only be compared within the same search. Snippets are not full profiles: call get_profile
    before judging or quoting anyone. Several short searches from different angles (job title synonyms,
    qualifications, related roles) find more than one long query."""
    cfg = _role(role)
    q = (query or "").strip()
    if not q:
        raise ToolError("query is empty. Describe what to look for, e.g. 'credit controller aged debt'.")
    texts, meta = _profiles()
    wanted = filters.model_dump(exclude_none=True) if filters else {}
    where, used = build_where(cfg["area_flag"], wanted)
    pool = {pid for pid, m in meta.items()
            if python_filter(m, cfg["area_flag"]) and all(bool(m.get(k)) == v for k, v in used.items())}
    if not pool:
        raise ToolError(f"No candidates for role '{role}' match the filters {used}. Remove or loosen a filter.")
    with _SEARCH_LOCK:  # one search at a time: see _SEARCH_LOCK
        col = _collection()
        embed = _query_embedder()
        try:
            ranked = hybrid_rank(col, q, where, texts, pool, embed=embed)[:n_results]
        except Exception as e:  # anything unanticipated: say what failed instead of the SDK's generic message
            log.exception("search failed")
            raise ToolError(f"Search failed ({type(e).__name__}: {str(e)[:200]}). The embedding model (about 80 MB) "
                            "downloads on the first search on a machine; check the server log.") from e
    log.info("search_candidates role=%s n=%d filters=%s pool=%d returned=%d", role, n_results, used, len(pool), len(ranked))
    return {"role": role, "query": q, "filters_applied": used, "pool_size": len(pool), "results": [
        {"rank": i, "id": pid, "title": meta[pid].get("current_title") or "", "employer": meta[pid].get("current_company") or "",
         "location": meta[pid].get("location_text") or "", "qualifications": _quals(meta[pid]),
         "snippet": texts[pid][:SNIPPET_CHARS], "score": round(score, 6)}
        for i, (pid, score) in enumerate(ranked, 1)]}


@mcp.tool(annotations=READ_ONLY)
def get_profile(
    # str or int: ids look numeric, and clients send them as numbers (the Inspector did, 14/10, and was rejected)
    candidate_id: Annotated[str | int, Field(description="Candidate id as returned by search_candidates")],
) -> Profile:
    """Return one candidate's full profile by id, as given by search_candidates: current role, headline,
    qualifications as stated, location and previous roles. Read this before recommending a candidate, and
    quote only text that appears in it. Unknown ids return an error."""
    pid = str(candidate_id).strip()
    texts, meta = _profiles()
    if pid not in texts or meta[pid].get("client_staff"):  # client staff look exactly like unknown ids
        raise ToolError(f"No candidate with id '{pid}'. Use an id returned by search_candidates.")
    m = meta[pid]
    return {"id": pid, "title": m.get("current_title") or "", "employer": m.get("current_company") or "",
            "location": m.get("location_text") or "", "qualifications": _quals(m), "profile": texts[pid]}


# ---------------------------------------------------------------------------- resource
@mcp.resource("jd://{role}", mime_type="text/plain",
              description="The full job advert for a role. Read it before searching so your searches "
                          "reflect what the advert asks for.")
def job_description(role: str) -> str:
    if role not in ROLES:
        raise ResourceNotFoundError(f"No job advert for role '{role}'. Known roles: {', '.join(sorted(ROLES))}.")
    path = JDS / ROLES[role]["jd"]
    if not path.is_file():
        raise ResourceNotFoundError(f"The advert file for '{role}' is missing ({path.name}).")
    return path.read_text(encoding="utf-8")


# ---------------------------------------------------------------------------- prompt
@mcp.prompt(description="Shortlist candidates for a role, with evidence quoted from each profile.")
def shortlist_for_role(role: str, how_many: int = 3) -> str:
    if role not in ROLES:
        raise MCPError(INVALID_PARAMS, f"Unknown role '{role}'. Known roles: {', '.join(sorted(ROLES))}.")
    if not 1 <= int(how_many) <= MAX_RESULTS:
        raise MCPError(INVALID_PARAMS, f"how_many must be between 1 and {MAX_RESULTS}.")
    return f"""Shortlist the {how_many} best candidates for the role '{role}' from the candidate-rag pool.

1. Read the job advert at jd://{role} first.
2. Call search_candidates for role '{role}' at least three times, from genuinely different angles: the job
   title and its synonyms, the qualifications the advert asks for (you can use the qualification filters),
   and adjacent roles that could fit.
3. Call get_profile for every candidate before you recommend them. Do not judge anyone from a snippet.
4. Apply these rules, agreed with the recruiter:
   - Favour no false positives over coverage. If in doubt, do not call a candidate strong.
   - Only count qualifications the profile states. "Likely held" or an unspecified level is weaker evidence.
   - The right job title with no visible qualification, where the role asks for one, is possible, not strong.
   - For credit control: a current or recent business-to-business credit controller can be strong in any
     industry; professional-services experience (partner billing, WIP) ranks higher but is not required.
     Consumer or finance-house collections, and credit manager titles, are possible at most.
5. For each candidate you recommend, give: id, current title, strong or possible, one or two sentences of
   reasoning, and an evidence quote copied exactly from their get_profile text.
6. If fewer than {how_many} candidates are strong, say so plainly rather than padding the list.
"""


# ---------------------------------------------------------------------------- HTTP transport (Part 3)
@mcp.custom_route("/health", methods=["GET"])
async def health(_request: Request) -> JSONResponse:
    """For the load balancer. Outside the MCP endpoint, so it isn't subject to the Host check: a load
    balancer's health check names the task's own IP address as the host. The server only starts listening
    after _warm_up() has succeeded, so any answer at all means the search path is ready."""
    return JSONResponse({"status": "ok", "mode": MODE})


def _host_patterns(extra: str) -> list[str]:
    """Allowed Host header values: always the local ones, plus each public hostname given (with or without a
    port; a bare hostname also matches it with any port)."""
    hosts = list(LOCAL_HOSTS)
    for h in (x.strip().lower() for x in extra.split(",")):
        if h and h not in hosts:
            hosts += [h] if ":" in h else [h, f"{h}:*"]
    return hosts


def http_app(allowed_hosts: str | None = None, allowed_origins: str | None = None):
    """The Starlette app for the Streamable HTTP transport, built from the same MCPServer as stdio.
    Stateless: no session is kept between requests, so two copies behind a load balancer (as during a
    canary deploy) answer any request alike. JSON replies: one plain response per call, no event stream
    held open through the load balancer."""
    hosts = _host_patterns(os.environ.get("MCP_ALLOWED_HOSTS", "") if allowed_hosts is None else allowed_hosts)
    extra = os.environ.get("MCP_ALLOWED_ORIGINS", "") if allowed_origins is None else allowed_origins
    origins = ALLOWED_ORIGINS + [o.strip() for o in extra.split(",") if o.strip()]
    security = TransportSecuritySettings(enable_dns_rebinding_protection=True,
                                         allowed_hosts=hosts, allowed_origins=origins)
    return mcp.streamable_http_app(streamable_http_path=MCP_PATH, stateless_http=True, json_response=True,
                                   transport_security=security)


def _warm_up() -> None:
    """Open the profiles and the index copy and embed one query, so the first real search is fast and a
    broken image fails here, at start-up, instead of on a user's first call."""
    t0 = time.monotonic()
    texts, _ = _profiles()
    col = _collection()
    col.query(query_embeddings=[_query_embedder()("warm-up")], n_results=1)  # loads the model; logs no search line
    log.info("warm: %d profiles, index open, model loaded (%.1f s)", len(texts), time.monotonic() - t0)


# ---------------------------------------------------------------------------- shutdown and entry point
def _stop_now(signum, _frame) -> None:
    """Ctrl+C, SIGTERM (how ECS stops a task) or, on Windows, Ctrl+Break: clean up and exit at once.
    stdio: the SDK's stdio transport reads stdin on a worker thread, so a normal KeyboardInterrupt leaves the
    process waiting for that thread until stdin closes (measured 14/10: no exit within 10 s of Ctrl+C).
    HTTP: uvicorn catches the signal first, finishes in-flight requests, then re-raises it, which lands
    here. A read-only server has nothing to save, so it removes its temporary index copy and exits."""
    log.info("stopping (signal %s)", signum)
    _cleanup()
    log.info("stopped")
    logging.shutdown()
    os._exit(0)


def _cleanup() -> None:
    """Drop the collection, release Chroma's files and delete the temporary index copy."""
    from search import close_temp_copies
    _collection.cache_clear()
    _query_embedder.cache_clear()
    left = close_temp_copies()
    if left:
        log.warning("could not remove temporary index copy (files still open?): %s", ", ".join(left))


def main(argv: list[str] | None = None) -> int:
    import argparse
    import signal
    parser = argparse.ArgumentParser(description="candidate-rag MCP server (read-only)")
    parser.add_argument("--http", action="store_true",
                        help="serve Streamable HTTP at /mcp instead of stdio (or set MCP_TRANSPORT=http)")
    args = parser.parse_args(argv)
    http = args.http or os.environ.get("MCP_TRANSPORT", "").strip().lower() == "http"

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK exists on Windows only
        if hasattr(signal, name):
            signal.signal(getattr(signal, name), _stop_now)

    if not http:
        log.info("starting: mode=%s data=%s jds=%s (read-only, stdio)", MODE, DATA, JDS)
        mcp.run(transport="stdio")  # returns when the client closes stdin
        _cleanup()
        log.info("stopped (client closed the connection)")
        return 0

    host = os.environ.get("MCP_HOST", "127.0.0.1")
    port = int(os.environ.get("MCP_PORT", str(DEFAULT_PORT)))
    log.info("starting: mode=%s data=%s jds=%s (read-only, streamable HTTP on %s:%d%s, stateless)",
             MODE, DATA, JDS, host, port, MCP_PATH)
    log.info("allowed hosts: %s", ", ".join(_host_patterns(os.environ.get("MCP_ALLOWED_HOSTS", ""))))
    try:
        _warm_up()
    except Exception as e:  # missing index, missing profiles, model failure: refuse to start, say why
        log.error("not starting: %s", e)
        _cleanup()
        return 1
    import uvicorn
    uvicorn.run(http_app(), host=host, port=port, log_level="info")  # uvicorn handles SIGINT/SIGTERM
    _cleanup()
    log.info("stopped")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
src/ingest.py  -  Step 2 of candidate-rag: build one profile document per candidate.

Reads sourcing-core READ-ONLY and writes data/profiles.jsonl, one record per candidate:

    {"id": "<sourcing-core person_id>", "text": "...", "metadata": {...}}

Scope (decided 08/10): the 145 LinkedIn people that sourcing-core scored. The 88 team-page
people are left out (never scored, almost no location).

text      What gets embedded. Most important first: current role, headline, previous roles,
          qualifications as stated, location, source. Kept short because the embedding model
          reads roughly the first 256 word pieces and silently ignores the rest.
metadata  Structured fields for hard filters (location flags, qualification flags, client-staff
          flag) plus sourcing-core's verdicts under eval_* keys. The eval_* keys are for
          evaluation ONLY: they never go into the text and must never be used in retrieval,
          otherwise the evaluation marks its own homework (data leakage).

Read-only on sourcing-core: the database is copied to a temp folder (deleted afterwards) and
the copy is opened with mode=ro and query_only, so nothing in sourcing-core is ever opened for
writing. The only file written is data/profiles.jsonl inside candidate-rag.

Usage (from the candidate-rag folder, venv active):
    python src/ingest.py
    python src/ingest.py --root "C:\\Users\\Admin\\Downloads\\sourcing-core"
"""

from __future__ import annotations

import argparse
import json
import re
import shutil
import sqlite3
import statistics
import sys
import tempfile
from collections import Counter, defaultdict
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
DEFAULT_ROOT = PROJECT.parent / "sourcing-core"
OUT = PROJECT / "data" / "profiles.jsonl"
CLIENT_CONFIG = PROJECT / "data" / "client.json"   # private, git-ignored

ROLES = ("credit-controller", "semi-senior", "client-manager")
LABEL_WORDS = re.compile(r"\b(strong|possible|reject|rejected)\b", re.I)
MAX_WORDS = 150          # well inside the ~256 word-piece window, allowing for long firm names
MAX_PREVIOUS = 4         # previous roles listed before trimming for length

# The client's own details live in data/client.json (git-ignored), copied from sourcing-core's client
# config rather than imported, so that nothing in sourcing-core runs and nothing client-specific is
# published. Keys:
#   area_core           towns within roughly 20 miles of the main office (loc_area_core)
#   area_wide_extra     extra towns for the role covering a second office (loc_area_wide = core + extra)
#   client_staff_regex  matches the client's own firm in an employer name or headline (client_staff)
def load_client_config() -> tuple[list[str], list[str], re.Pattern]:
    if not CLIENT_CONFIG.is_file():
        raise SystemExit(f"ERROR: {CLIENT_CONFIG} not found. It holds the client's town lists and name "
                         "pattern and is deliberately not in the repository.")
    cfg = json.loads(CLIENT_CONFIG.read_text(encoding="utf-8"))
    core = [t.lower() for t in cfg["area_core"]]
    return core, core + [t.lower() for t in cfg["area_wide_extra"]], re.compile(cfg["client_staff_regex"], re.I)

QUAL_NAMES = {
    "AAT_QUALIFIED": "AAT qualified", "AAT_L3": "AAT Level 3", "AAT_UNSPEC": "AAT (level not stated)",
    "ACA": "ACA", "ACCA": "ACCA", "ATT": "ATT", "CFAB": "CFAB", "CTA": "CTA", "CICM": "CICM",
    "ICAS_CA": "ICAS CA", "CHARTERED_UNSPEC": "chartered accountancy (body not stated)",
}
STATUS_WORDS = {
    "held": "", "likely_held": "likely held", "studying": "studying",
    "part_qualified": "part-qualified", "finalist": "finalist",
}
IN_PROGRESS = {"studying", "part_qualified", "finalist"}


def open_copy(db: Path, workdir: Path) -> sqlite3.Connection:
    shutil.copy2(db, workdir / db.name)
    wal = db.with_name(db.name + "-wal")
    if wal.exists():
        shutil.copy2(wal, workdir / wal.name)
    con = sqlite3.connect((workdir / db.name).as_uri() + "?mode=ro", uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


def clean(s) -> str:
    return " ".join(str(s).split()) if s else ""


def in_area(location: str, towns: list[str]) -> bool:
    loc = location.lower()
    return any(re.search(r"\b" + re.escape(t) + r"\b", loc) for t in towns)


def role_phrase(title: str, company: str, start: str, end: str, current: bool) -> str:
    bits = title or "Role not stated"
    if company:
        bits += f", {company}"
    span = " - ".join(x for x in (start, "present" if current else end) if x)
    return f"{bits} ({span})" if span else bits


def qual_phrase(code: str, status: str) -> str:
    name = QUAL_NAMES.get(code, code)
    word = STATUS_WORDS.get(status, status.replace("_", " ") if status else "")
    return f"{name} ({word})" if word else name


def build_text(p: dict, positions: list[dict], quals: list[dict]) -> tuple[str, int]:
    """Return (text, number of previous roles dropped to stay under MAX_WORDS)."""
    current = [x for x in positions if x["is_current"]]
    # Undated previous entries go after dated ones. On 08/10, 3 people (ids 68, 70, 129) had an
    # undated, non-current entry stored at seq 0, ahead of their dated roles.
    previous = sorted((x for x in positions if not x["is_current"]),
                      key=lambda x: (not (x["start"] or x["end"]), x["seq"]))

    lines = []
    if current:
        lines.append("Current role: " + "; ".join(
            role_phrase(x["title"], x["company"], x["start"], x["end"], True) for x in current[:2]) + ".")
    else:
        lines.append("Current role: none listed.")
    if p["headline"]:
        lines.append(f"Headline: {p['headline'].rstrip('. ')}.")

    seen, qparts = set(), []
    for q in quals:
        ph = qual_phrase(q["code"], q["status"])
        if ph not in seen:
            seen.add(ph)
            qparts.append(ph)
    tail = [f"Qualifications: {', '.join(qparts) if qparts else 'none listed'}.",
            f"Location: {p['location'] or 'not stated'}.",
            "Source: LinkedIn."]

    # Previous roles go LAST: if anything is cut at the embedding window it should be old roles,
    # never qualifications or location.
    prev = [role_phrase(x["title"], x["company"], x["start"], x["end"], False) for x in previous[:MAX_PREVIOUS]]
    dropped = max(0, len(previous) - MAX_PREVIOUS)
    while True:
        body = lines + tail + ([f"Previous roles: {'; '.join(prev)}."] if prev else [])
        text = " ".join(body)
        if len(text.split()) <= MAX_WORDS or not prev:
            return text, dropped
        prev.pop()
        dropped += 1


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", default=str(DEFAULT_ROOT), help="sourcing-core folder")
    ap.add_argument("--samples", type=int, default=3, help="records to print for reading")
    ap.add_argument("--explain-order", action="store_true",
                    help="print seq, is_current and dates for people who break the ordering assumption")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    root = Path(args.root).expanduser().resolve()
    db = root / "data" / "core.sqlite"
    if not db.is_file():
        print(f"ERROR: no database at {db}")
        return 2
    area_core, area_wide, client_staff = load_client_config()
    if root in OUT.resolve().parents:
        print("ERROR: output would be inside sourcing-core. Refusing.")
        return 2

    with tempfile.TemporaryDirectory(prefix="ingest_", ignore_cleanup_errors=True) as tmp:
        con = open_copy(db, Path(tmp))
        try:
            people = {pid: {"headline": clean(hl), "location": clean(loc)}
                      for pid, hl, loc in con.execute(
                          "SELECT person_id, headline, location_text FROM people")}
            linkedin = {pid for (pid,) in con.execute(
                "SELECT DISTINCT person_id FROM contacts WHERE kind = 'linkedin_url'")}
            scored = {pid for (pid,) in con.execute("SELECT DISTINCT person_id FROM matches")}
            positions = defaultdict(list)
            for pid, seq, title, company, start, end, cur in con.execute(
                    "SELECT person_id, seq, title, company, start_text, end_text, is_current "
                    "FROM positions ORDER BY person_id, is_current DESC, seq ASC"):
                positions[pid].append({"seq": seq, "title": clean(title), "company": clean(company),
                                       "start": clean(start), "end": clean(end), "is_current": bool(cur)})
            quals = defaultdict(list)
            for pid, code, status in con.execute(
                    "SELECT person_id, code, status FROM qualifications ORDER BY person_id, code"):
                quals[pid].append({"code": code or "", "status": status or ""})
            labels = {}
            for role, pid, verdict, score in con.execute(
                    "SELECT role_id, person_id, verdict, score FROM matches"):
                labels[(role, pid)] = (clean(verdict).lower(), score)
        finally:
            con.close()

    ids = sorted(pid for pid in people if pid in linkedin and pid in scored)
    records, dropped_total, seq_oddities, odd_ids = [], 0, 0, []
    for pid in ids:
        p, pos, qs = people[pid], positions.get(pid, []), quals.get(pid, [])
        cur = [x for x in pos if x["is_current"]]
        dated = [x for x in pos if x["start"] or x["end"]]
        if cur and dated and min(x["seq"] for x in cur) > min(x["seq"] for x in dated):
            seq_oddities += 1
            odd_ids.append(pid)
        text, dropped = build_text(p, pos, qs)
        dropped_total += dropped
        codes = {q["code"] for q in qs}

        def has(codes_wanted, statuses):
            return any(q["code"] in codes_wanted and q["status"] in statuses for q in qs)

        employers = " ".join([x["company"] for x in cur] + [p["headline"]])
        meta = {
            "person_id": pid,
            "source": "linkedin",
            "location_text": p["location"],
            "loc_area_core": in_area(p["location"], area_core),
            "loc_area_wide": in_area(p["location"], area_wide),
            "loc_uk_only": p["location"].lower() in ("united kingdom", "uk", "england, united kingdom"),
            "current_title": cur[0]["title"] if cur else "",
            "current_company": cur[0]["company"] if cur else "",
            "qual_codes": "|".join(sorted(codes)),
            "has_any_qual": bool(qs),
            "aat_qualified": has({"AAT_QUALIFIED"}, {"held", "likely_held"}),
            "aca_acca_held": has({"ACA", "ACCA"}, {"held"}),
            "aca_acca_in_progress": has({"ACA", "ACCA"}, IN_PROGRESS),
            "cicm": "CICM" in codes,
            "client_staff": bool(client_staff.search(employers)),
            "n_words": len(text.split()),
        }
        for role in ROLES:
            key = role.replace("-", "_")
            verdict, score = labels.get((role, pid), ("unscored", None))
            meta[f"eval_label_{key}"] = verdict
            meta[f"eval_score_{key}"] = score if score is not None else -1
        records.append({"id": str(pid), "text": text, "metadata": meta})

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8") as f:
        for r in records:
            f.write(json.dumps(r, ensure_ascii=False) + "\n")

    # ------------------------------------------------------------------ checks
    words = [r["metadata"]["n_words"] for r in records]
    # A label word inside the person's own LinkedIn headline ("strong communicator") is their wording,
    # not sourcing-core's verdict. Anywhere else in the text it can only have come from the template,
    # which would be a real leak.
    leaks, headline_words = [], []
    for r in records:
        hl = people[int(r["id"])]["headline"]
        rest = r["text"].replace(f"Headline: {hl.rstrip('. ')}.", "") if hl else r["text"]
        if LABEL_WORDS.search(rest):
            leaks.append(r["id"])
        elif LABEL_WORDS.search(r["text"]):
            headline_words.append(r["id"])
    dup = [k for k, v in Counter(r["id"] for r in records).items() if v > 1]
    m = [r["metadata"] for r in records]
    print("=" * 70)
    print("STEP 2 CHECK  (safe to paste: counts only)")
    print("=" * 70)
    print(f"sourcing-core people {len(people)}; LinkedIn {len(linkedin)}; scored {len(scored)}")
    print(f"records written: {len(records)}  -> {OUT}")
    print(f"duplicate ids: {len(dup)}")
    print(f"words per text: min {min(words)}, median {statistics.median(words)}, max {max(words)} "
          f"(cap {MAX_WORDS})")
    print(f"previous roles dropped to fit the cap: {dropped_total}")
    print(f"people whose current role is not their lowest DATED seq (ordering assumption broken): {seq_oddities}"
          + (f"  ids: {', '.join(map(str, odd_ids[:20]))}" if odd_ids else ""))
    if args.explain_order and odd_ids:
        print("  positions for those people as stored (seq, is_current, start, end; no titles or employers):")
        for pid in odd_ids:
            rows = sorted(positions[pid], key=lambda x: x["seq"])
            print(f"    id {pid}: " + " | ".join(
                f"seq {x['seq']} cur {int(x['is_current'])} {x['start'] or '?'} - {x['end'] or '?'}" for x in rows))
    print(f"LEAK CHECK - label words outside the headline (must be 0): {len(leaks)}"
          + (f"  ids: {', '.join(leaks[:20])}" if leaks else ""))
    print(f"label words inside a LinkedIn headline (the person's own wording, check by eye): "
          f"{len(headline_words)}" + (f"  ids: {', '.join(headline_words[:20])}" if headline_words else ""))
    print(f"no current role: {sum(1 for x in m if not x['current_title'])}; "
          f"no qualifications: {sum(1 for x in m if not x['has_any_qual'])}")
    print(f"location: core area {sum(x['loc_area_core'] for x in m)}, "
          f"wide area {sum(x['loc_area_wide'] for x in m)}, "
          f"'United Kingdom' only {sum(x['loc_uk_only'] for x in m)}, "
          f"neither {sum(1 for x in m if not x['loc_area_wide'] and not x['loc_uk_only'])}")
    print(f"qualification flags: AAT qualified {sum(x['aat_qualified'] for x in m)}, "
          f"ACA/ACCA held {sum(x['aca_acca_held'] for x in m)}, "
          f"ACA/ACCA in progress {sum(x['aca_acca_in_progress'] for x in m)}, CICM {sum(x['cicm'] for x in m)}")
    print(f"client staff (current employer is the client): {sum(x['client_staff'] for x in m)}")
    for role in ROLES:
        key = role.replace("-", "_")
        c = Counter(x[f"eval_label_{key}"] for x in m)
        print(f"eval labels {role}: " + ", ".join(f"{k}={v}" for k, v in sorted(c.items())))

    if args.samples:
        print("")
        print("=" * 70)
        print(f"{args.samples} SAMPLE RECORDS  (read these yourself; do NOT paste them, they are real people)")
        print("=" * 70)
        step = max(1, len(records) // args.samples)
        for r in records[::step][:args.samples]:
            print(f"\nid {r['id']}  ({r['metadata']['n_words']} words)")
            print(r["text"])
    return 0


if __name__ == "__main__":
    sys.exit(main())

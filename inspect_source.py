"""
inspect_source.py  -  Step 0 of candidate-rag.

Read-only inspection of sourcing-core. It never writes to sourcing-core:
  * Each SQLite database is copied to a temporary folder (deleted afterwards)
    and the copy is opened with mode=ro and PRAGMA query_only=ON, so SQLite
    never creates -shm / -wal files inside sourcing-core.
  * Every other file is opened for reading only.
  * The only file written is the report, which must sit OUTSIDE sourcing-core
    (the script refuses to run otherwise).

What it reports:
  1. Every SQLite database under sourcing-core.
  2. For each database: every table and view with its row count and columns,
     any long-text columns (a sign that profile or job text is stored),
     every column holding strong / possible / reject values with counts,
     role / pool / source breakdowns, and two sample rows from each table
     that looks like it holds candidates.
  3. Files that mention the roles (the client config holding role definitions).
  4. Shortlist output files, with their label counts.

Direct identifiers in sample rows (names, emails, phones, URLs) are masked
by default. Pass --show-identifiers to print them in full.

Standard library only. Usage:
    python inspect_source.py --root "C:\\path\\to\\sourcing-core"
"""

from __future__ import annotations

import argparse
import csv
import json
import re
import shutil
import sqlite3
import sys
import tempfile
from collections import Counter
from pathlib import Path

SKIP_DIRS = {".venv", "venv", "env", ".git", "__pycache__", "node_modules",
             ".pytest_cache", ".mypy_cache", "site-packages"}
DB_EXTS = {".sqlite", ".sqlite3", ".db"}
TEXT_EXTS = {".yaml", ".yml", ".json", ".toml", ".py", ".txt", ".md", ".ini", ".cfg"}
CONFIG_EXTS = {".yaml", ".yml", ".json", ".toml", ".ini", ".cfg"}
OUTPUT_EXTS = {".csv", ".json", ".html", ".xlsx", ".jsonl"}

LABEL_VALUES = ("strong", "possible", "reject", "rejected", "maybe", "weak")
ROLE_TERMS = ("credit controller", "credit control", "semi-senior", "semi senior",
              "client manager", "senior accountant")
CANDIDATE_TABLE_RE = re.compile(r"person|people|candidate|profile|member|contact", re.I)
GROUP_COL_RE = re.compile(r"role|pool|channel|source|origin|job|vacanc", re.I)
IDENTIFIER_COL_RE = re.compile(
    r"(^|_)(name|first_?name|last_?name|surname|full_?name|email|phone|mobile|tel|linkedin|url|link|"
    r"public_identifier|slug|address|street|photo|image|avatar)($|_|s$)", re.I)
# Masked whatever the column is called (contacts.value held LinkedIn URLs and slipped through v1).
IDENTIFIER_VALUE_RE = re.compile(
    r"https?://|www\.|linkedin\.com|@[a-z0-9-]+\.[a-z]|\bACoA[A-Za-z0-9_-]{10,}|"
    r"(\+44|\b0)\s?\d{2,4}[\s-]?\d{3,4}[\s-]?\d{3,4}", re.I)
OUTPUT_NAME_RE = re.compile(r"shortlist|result|export|output|ranking|scored", re.I)

MAX_CELL = 160          # characters shown per sample cell
MAX_CONFIG_LINES = 150  # lines shown per matching config file
MAX_GROUPS = 25         # distinct values shown per breakdown


class Report:
    def __init__(self) -> None:
        self.lines: list[str] = []

    def __call__(self, text: str = "") -> None:
        self.lines.append(text)
        print(text)

    def h(self, title: str) -> None:
        self("")
        self("=" * 78)
        self(title)
        self("=" * 78)


def q(ident: str) -> str:
    """Quote an SQLite identifier."""
    return '"' + ident.replace('"', '""') + '"'


def show(value, col: str, show_ids: bool) -> str:
    if value is None:
        return "NULL"
    text = str(value).replace("\r", " ").replace("\n", " | ")
    if not show_ids and text.strip() and (IDENTIFIER_COL_RE.search(col)
                                          or IDENTIFIER_VALUE_RE.search(text)):
        return f"<masked, {len(text)} chars>"
    return text if len(text) <= MAX_CELL else text[:MAX_CELL] + f"... (+{len(text) - MAX_CELL} chars)"


def walk(root: Path):
    stack = [root]
    while stack:
        d = stack.pop()
        try:
            entries = sorted(d.iterdir(), key=lambda p: p.name.lower())
        except (PermissionError, OSError):
            continue
        for p in entries:
            if p.is_dir():
                if p.name not in SKIP_DIRS:
                    stack.append(p)
            elif p.is_file():
                yield p


def open_ro(path: Path, workdir: Path) -> sqlite3.Connection:
    """Copy the database (and any -wal file) into a temp folder and open the copy.

    Opening a WAL-mode database directly, even with mode=ro, makes SQLite create
    -shm and -wal files beside it, which would be a write into sourcing-core.
    Copying first means nothing in sourcing-core is ever opened for writing.
    """
    dest = workdir / path.name
    shutil.copy2(path, dest)
    for suffix in ("-wal",):
        side = path.with_name(path.name + suffix)
        if side.exists():
            shutil.copy2(side, workdir / side.name)
    uri = dest.as_uri() + "?mode=ro"
    con = sqlite3.connect(uri, uri=True)
    con.execute("PRAGMA query_only = ON")
    return con


def inspect_db(path: Path, r: Report, show_ids: bool) -> None:
    r.h(f"DATABASE  {path}")
    with tempfile.TemporaryDirectory(prefix="inspect_source_",
                                     ignore_cleanup_errors=True) as tmp:
        try:
            con = open_ro(path, Path(tmp))
        except (sqlite3.Error, OSError) as e:
            r(f"  Could not open a read-only copy: {e}")
            return
        try:
            _inspect_db(con, r, show_ids)
        finally:
            con.close()


def _inspect_db(con: sqlite3.Connection, r: Report, show_ids: bool) -> None:
    try:
        objs = con.execute(
            "SELECT name, type FROM sqlite_master WHERE type IN ('table','view') "
            "AND name NOT LIKE 'sqlite_%' ORDER BY type, name").fetchall()
    except sqlite3.Error as e:
        r(f"  Could not read the schema: {e}")
        return

    # 1. Tables and counts
    r("")
    r("Tables and views (row counts):")
    counts: dict[str, int] = {}
    for name, typ in objs:
        try:
            n = con.execute(f"SELECT COUNT(*) FROM {q(name)}").fetchone()[0]
        except sqlite3.Error as e:
            n = -1
            r(f"  {typ:5} {name}: count failed ({e})")
            continue
        counts[name] = n
        r(f"  {typ:5} {name:40} {n:>8}")

    # 2. Per-table detail
    for name, typ in objs:
        if name not in counts:
            continue
        cols = con.execute(f"PRAGMA table_info({q(name)})").fetchall()
        colnames = [c[1] for c in cols]
        r("")
        r(f"--- {typ} {name}  ({counts[name]} rows)")
        r("  columns: " + ", ".join(f"{c[1]} {c[2] or ''}".strip() for c in cols))
        if counts[name] == 0:
            continue

        # Long text columns
        longs = []
        for c in colnames:
            try:
                mx = con.execute(f"SELECT MAX(LENGTH({q(c)})) FROM {q(name)} "
                                 f"WHERE typeof({q(c)}) = 'text'").fetchone()[0]
            except sqlite3.Error:
                mx = None
            if mx and mx > 400:
                longs.append(f"{c} (max {mx} chars)")
        if longs:
            r("  long-text columns: " + ", ".join(longs))

        # Label columns
        label_cols = []
        placeholders = ",".join("?" * len(LABEL_VALUES))
        for c in colnames:
            try:
                rows = con.execute(
                    f"SELECT LOWER(TRIM({q(c)})), COUNT(*) FROM {q(name)} "
                    f"WHERE typeof({q(c)}) = 'text' AND LOWER(TRIM({q(c)})) IN ({placeholders}) "
                    f"GROUP BY 1 ORDER BY 2 DESC", LABEL_VALUES).fetchall()
            except sqlite3.Error:
                rows = []
            if rows:
                label_cols.append(c)
                r(f"  LABEL VALUES in '{c}': " + ", ".join(f"{v}={n}" for v, n in rows))
                try:
                    allv = con.execute(
                        f"SELECT {q(c)}, COUNT(*) FROM {q(name)} GROUP BY 1 "
                        f"ORDER BY 2 DESC LIMIT {MAX_GROUPS}").fetchall()
                    r(f"    all values of '{c}': " + ", ".join(f"{v!r}={n}" for v, n in allv))
                except sqlite3.Error:
                    pass

        # Role / pool / source breakdowns
        group_cols = [c for c in colnames if GROUP_COL_RE.search(c)
                      and not IDENTIFIER_COL_RE.search(c)]
        if CANDIDATE_TABLE_RE.search(name) or label_cols:
            for g in group_cols:
                try:
                    distinct = con.execute(
                        f"SELECT COUNT(DISTINCT {q(g)}) FROM {q(name)}").fetchone()[0]
                except sqlite3.Error:
                    continue
                if distinct == 0 or distinct > 60:
                    continue
                rows = con.execute(
                    f"SELECT {q(g)}, COUNT(*) FROM {q(name)} GROUP BY 1 "
                    f"ORDER BY 2 DESC LIMIT {MAX_GROUPS}").fetchall()
                r(f"  by '{g}': " + ", ".join(f"{show(v, g, True)}={n}" for v, n in rows))
                for lc in label_cols:
                    if lc == g:
                        continue
                    xt = con.execute(
                        f"SELECT {q(g)}, LOWER(TRIM({q(lc)})), COUNT(*) FROM {q(name)} "
                        f"GROUP BY 1, 2 ORDER BY 1, 3 DESC").fetchall()
                    r(f"  '{g}' x '{lc}':")
                    for gv, lv, n in xt[:80]:
                        r(f"    {show(gv, g, True):40} {str(lv):12} {n}")

        # Samples from candidate-looking tables
        if CANDIDATE_TABLE_RE.search(name):
            r("  sample rows:")
            for i, row in enumerate(con.execute(f"SELECT * FROM {q(name)} LIMIT 2"), 1):
                r(f"    [{i}]")
                for c, v in zip(colnames, row):
                    r(f"      {c}: {show(v, c, show_ids)}")

    if not any(CANDIDATE_TABLE_RE.search(n) for n in counts):
        biggest = max(counts, key=counts.get, default=None)
        if biggest:
            r("")
            r(f"No table name looks like candidates; sample rows from largest table '{biggest}':")
            colnames = [c[1] for c in con.execute(f"PRAGMA table_info({q(biggest)})")]
            for i, row in enumerate(con.execute(f"SELECT * FROM {q(biggest)} LIMIT 2"), 1):
                r(f"    [{i}]")
                for c, v in zip(colnames, row):
                    r(f"      {c}: {show(v, c, show_ids)}")


def read_text(p: Path) -> str | None:
    try:
        if p.stat().st_size > 5_000_000:
            return None
        return p.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None


def inspect_role_files(files: list[Path], root: Path, r: Report) -> None:
    r.h("FILES MENTIONING THE ROLES (role definitions / client config)")
    found = False
    for p in files:
        if p.suffix.lower() not in TEXT_EXTS:
            continue
        text = read_text(p)
        if not text:
            continue
        low = text.lower()
        hits = [t for t in ROLE_TERMS if t in low]
        if not hits:
            continue
        found = True
        rel = p.relative_to(root)
        lines = text.splitlines()
        hit_lines = [i + 1 for i, ln in enumerate(lines)
                     if any(t in ln.lower() for t in ROLE_TERMS)]
        r("")
        r(f"* {rel}  ({len(lines)} lines; terms: {', '.join(hits)}; "
          f"lines {', '.join(map(str, hit_lines[:12]))}{' ...' if len(hit_lines) > 12 else ''})")
        is_config = (p.suffix.lower() in CONFIG_EXTS
                     or re.search(r"config|client|role", str(rel), re.I))
        is_output = OUTPUT_NAME_RE.search(p.name) or "test" in str(rel).lower()
        # Never print anything stored under a data folder: in v1 this dumped raw Apify
        # profile JSON (names, LinkedIn URLs) because data\apify_probe\role_1.json matched "role".
        in_data = any(part.lower() == "data" for part in rel.parts)
        if is_config and not is_output and not in_data:
            r(f"  ---- contents (first {MAX_CONFIG_LINES} lines) ----")
            for ln in lines[:MAX_CONFIG_LINES]:
                r("  | " + ln)
            if len(lines) > MAX_CONFIG_LINES:
                r(f"  | ... {len(lines) - MAX_CONFIG_LINES} more lines")
    if not found:
        r("  No file mentions the role names.")


def label_counts(records: list[dict]) -> dict[str, Counter]:
    out: dict[str, Counter] = {}
    for rec in records:
        for k, v in rec.items():
            if isinstance(v, str) and v.strip().lower() in LABEL_VALUES:
                out.setdefault(k, Counter())[v.strip().lower()] += 1
    return out


def inspect_outputs(files: list[Path], root: Path, r: Report, show_ids: bool) -> None:
    r.h("SHORTLIST / OUTPUT FILES")
    cands = [p for p in files if p.suffix.lower() in OUTPUT_EXTS
             and OUTPUT_NAME_RE.search(p.name)]
    if not cands:
        r("  None found by name (shortlist/result/export/output/ranking/scored).")
        return
    for p in cands:
        rel = p.relative_to(root)
        r("")
        r(f"* {rel}  ({p.stat().st_size:,} bytes)")
        ext = p.suffix.lower()
        try:
            if ext == ".csv":
                with p.open(encoding="utf-8-sig", errors="replace", newline="") as f:
                    rows = list(csv.DictReader(f))
                r(f"  {len(rows)} rows; columns: {', '.join(rows[0].keys()) if rows else '(empty)'}")
                for col, cnt in label_counts(rows).items():
                    r(f"  LABEL VALUES in '{col}': " + ", ".join(f"{k}={v}" for k, v in cnt.most_common()))
                for col in (rows[0].keys() if rows else []):
                    if GROUP_COL_RE.search(col) and not IDENTIFIER_COL_RE.search(col):
                        c = Counter(row.get(col) for row in rows)
                        if len(c) <= 60:
                            r(f"  by '{col}': " + ", ".join(f"{k}={v}" for k, v in c.most_common(MAX_GROUPS)))
                if rows:
                    r("  sample row:")
                    for k, v in rows[0].items():
                        r(f"    {k}: {show(v, k or '', show_ids)}")
            elif ext in (".json", ".jsonl"):
                text = read_text(p)
                if text is None:
                    r("  too large to read")
                    continue
                if ext == ".jsonl":
                    data = [json.loads(x) for x in text.splitlines() if x.strip()]
                else:
                    data = json.loads(text)
                if isinstance(data, dict):
                    r("  top-level keys: " + ", ".join(f"{k} ({type(v).__name__}"
                      f"{', ' + str(len(v)) if isinstance(v, (list, dict)) else ''})"
                      for k, v in data.items()))
                    lists = [v for v in data.values() if isinstance(v, list)
                             and v and isinstance(v[0], dict)]
                    records = [x for lst in lists for x in lst]
                elif isinstance(data, list):
                    records = [x for x in data if isinstance(x, dict)]
                    r(f"  list of {len(data)} items")
                else:
                    records = []
                if records:
                    r("  record keys: " + ", ".join(records[0].keys()))
                    for col, cnt in label_counts(records).items():
                        r(f"  LABEL VALUES in '{col}': " + ", ".join(f"{k}={v}" for k, v in cnt.most_common()))
            else:
                r("  (not parsed; listed for reference)")
        except Exception as e:  # report and carry on
            r(f"  could not parse: {type(e).__name__}: {e}")


def main() -> int:
    ap = argparse.ArgumentParser(description="Read-only inspection of sourcing-core.")
    ap.add_argument("--root", required=True, help="Path to the sourcing-core folder")
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "data" / "inspect_source_output.txt"),
                    help="Where to save the report (must be outside sourcing-core)")
    ap.add_argument("--show-identifiers", action="store_true",
                    help="Print names, emails, phones and URLs in sample rows unmasked")
    args = ap.parse_args()

    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    root = Path(args.root).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    if not root.is_dir():
        print(f"ERROR: --root is not a folder: {root}")
        return 2
    if out == root or root in out.parents:
        print(f"ERROR: the report path {out} is inside sourcing-core. Refusing, sourcing-core is read-only.")
        return 2

    r = Report()
    r(f"inspect_source.py  root={root}")
    r(f"identifiers in sample rows: {'SHOWN' if args.show_identifiers else 'masked'}")

    files = list(walk(root))
    dbs = [p for p in files if p.suffix.lower() in DB_EXTS]
    r.h("DATABASE FILES FOUND")
    if not dbs:
        r("  None (.sqlite / .sqlite3 / .db) outside virtual environments.")
    for p in dbs:
        r(f"  {p.relative_to(root)}  ({p.stat().st_size:,} bytes)")

    for p in dbs:
        inspect_db(p, r, args.show_identifiers)
    inspect_role_files(files, root, r)
    inspect_outputs(files, root, r, args.show_identifiers)

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(r.lines) + "\n", encoding="utf-8")
    print("")
    print(f"Report saved to {out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
inspect_pools.py  -  Step 0, second pass of candidate-rag.

Answers the questions the first inspection raised, and prints COUNTS ONLY:
no names, URLs, headlines or employer names, so the output is safe to paste.

  1. Who is in sourcing-core: LinkedIn people, team-page people, both.
  2. What the matches table covers: which people were scored against which role,
     whether every person was scored, and whether anyone was added after scoring.
  3. The real per-role pools: which people came from which Apify search file
     (data/apify_pool and data/apify_probe), and their verdicts for that role.
  4. Whether the 23/09 credit-controller employer downgrade is reflected in the
     stored verdicts (50 strong looks like it predates it).
  5. How much there is to build a profile from, per origin.

Read-only on sourcing-core: the database is copied to a temp folder (deleted
afterwards) and the copy is opened with mode=ro and query_only; JSON files are
opened for reading. Nothing is written anywhere except the report, which must
sit outside sourcing-core.

Usage:
    python inspect_pools.py --root "C:\\Users\\Admin\\Downloads\\sourcing-core"
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

# Copied from sourcing-core's client config (credit-controller "employer_downgrade"),
# as printed by inspect_source.py on 08/10. Matched against employer names.
CC_DOWNGRADE = re.compile(r"credit union|financial services|\bfinance\b|\bloans?\b|lending|"
                          r"collections|debt recovery|arrears", re.I)
VERDICTS = ("strong", "possible", "reject")


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


def slug_of(url: str | None) -> str | None:
    if not url:
        return None
    u = url.strip().lower().split("?")[0].split("#")[0].rstrip("/")
    m = re.search(r"/in/([^/]+)", u)
    return m.group(1) if m else None


def vc(counter: Counter) -> str:
    return ", ".join(f"{k}={counter.get(k, 0)}" for k in VERDICTS) + \
        (f", other={sum(v for k, v in counter.items() if k not in VERDICTS)}"
         if any(k not in VERDICTS for k in counter) else "")


def median(xs):
    return statistics.median(xs) if xs else 0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--root", required=True)
    ap.add_argument("--out", default=str(Path(__file__).resolve().parent / "data" / "inspect_pools_output.txt"))
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")

    root = Path(args.root).expanduser().resolve()
    out = Path(args.out).expanduser().resolve()
    db = root / "data" / "core.sqlite"
    if not db.is_file():
        print(f"ERROR: no database at {db}")
        return 2
    if out == root or root in out.parents:
        print("ERROR: report path is inside sourcing-core. Refusing, sourcing-core is read-only.")
        return 2

    r = Report()
    r(f"inspect_pools.py  root={root}")

    with tempfile.TemporaryDirectory(prefix="inspect_pools_", ignore_cleanup_errors=True) as tmp:
        shutil.copy2(db, Path(tmp) / db.name)
        wal = db.with_name(db.name + "-wal")
        if wal.exists():
            shutil.copy2(wal, Path(tmp) / wal.name)
        con = sqlite3.connect((Path(tmp) / db.name).as_uri() + "?mode=ro", uri=True)
        con.execute("PRAGMA query_only = ON")
        try:
            run(con, root, r)
        finally:
            con.close()

    out.parent.mkdir(parents=True, exist_ok=True)
    out.write_text("\n".join(r.lines) + "\n", encoding="utf-8")
    print(f"\nReport saved to {out}")
    return 0


def run(con: sqlite3.Connection, root: Path, r: Report) -> None:
    # ---------------------------------------------------------------- 0. meta
    r.h("0. schema_meta")
    for k, v in con.execute("SELECT key, value FROM schema_meta"):
        r(f"  {k} = {v}")

    # ---------------------------------------------------------------- 1. origins
    people = {pid: dict(linkedin_id=lid, slug=slug_of(url), headline=hl, loc=loc, niche=niche,
                        first_seen=fs)
              for pid, lid, url, hl, loc, niche, fs in con.execute(
                  "SELECT person_id, linkedin_id, linkedin_url, headline, location_text, niche_id, first_seen "
                  "FROM people")}
    li_ids = {pid for (pid,) in con.execute(
        "SELECT DISTINCT person_id FROM contacts WHERE kind = 'linkedin_url'")}
    li_ids |= {pid for pid, p in people.items() if p["slug"]}
    tp_ids = {pid for (pid,) in con.execute(
        "SELECT DISTINCT person_id FROM person_sources WHERE source = 'team_page'")}

    def origin(pid: int) -> str:
        a, b = pid in li_ids, pid in tp_ids
        return "both" if a and b else "linkedin" if a else "team_page" if b else "neither"

    r.h("1. People by origin")
    oc = Counter(origin(p) for p in people)
    r(f"  people rows: {len(people)}")
    for k in ("linkedin", "team_page", "both", "neither"):
        r(f"  {k:10} {oc.get(k, 0)}")
    r("  niche_id: " + ", ".join(f"{k}={v}" for k, v in Counter(p['niche'] for p in people.values()).items()))
    ids = sorted(people)
    r(f"  person_id range: {ids[0]}..{ids[-1]}" if ids else "  (none)")
    for k in ("linkedin", "team_page", "both"):
        sub = [p for p in people if origin(p) == k]
        if sub:
            r(f"    {k}: person_id {min(sub)}..{max(sub)}")

    # ---------------------------------------------------------------- 2. matches
    r.h("2. What the matches table covers")
    rows = con.execute("SELECT role_id, person_id, LOWER(TRIM(verdict)), score, reasons, scored_at "
                       "FROM matches").fetchall()
    pairs = Counter((role, pid) for role, pid, *_ in rows)
    r(f"  rows: {len(rows)}; distinct (role, person): {len(pairs)}; "
      f"duplicated pairs: {sum(1 for c in pairs.values() if c > 1)}")
    scored = defaultdict(set)
    for role, pid, *_ in rows:
        scored[role].add(pid)
    all_scored = set().union(*scored.values()) if scored else set()
    r(f"  distinct people scored: {len(all_scored)}; same set for every role: "
      f"{len({frozenset(s) for s in scored.values()}) == 1}")
    r("  scored people by origin: " + ", ".join(f"{k}={v}" for k, v in
                                               Counter(origin(p) for p in all_scored).items()))
    unscored = [p for p in people if p not in all_scored]
    r("  people NEVER scored, by origin: " + (", ".join(f"{k}={v}" for k, v in
                                                     Counter(origin(p) for p in unscored).items()) or "none"))
    missing = [p for p in all_scored if p not in people]
    r(f"  matches rows pointing at a person_id not in people: {len(missing)}")

    r("")
    r("  per role: verdicts, by origin, score range, scored_at range")
    for role in sorted(scored):
        rr = [x for x in rows if x[0] == role]
        r(f"  {role}: {vc(Counter(x[2] for x in rr))}")
        for o in ("linkedin", "team_page", "both"):
            sub = [x for x in rr if origin(x[1]) == o]
            if sub:
                r(f"      {o:10} {vc(Counter(x[2] for x in sub))}")
        for v in VERDICTS:
            sc = [x[3] for x in rr if x[2] == v and x[3] is not None]
            if sc:
                r(f"      score {v:8} min {min(sc)} median {median(sc)} max {max(sc)}")
        ts = sorted(x[5] for x in rr if x[5])
        if ts:
            r(f"      scored_at {ts[0]} .. {ts[-1]}")
    last_scored = max((x[5] for x in rows if x[5]), default=None)
    if last_scored:
        later = [p for p, d in people.items() if d["first_seen"] and d["first_seen"] > last_scored]
        r(f"  people first seen AFTER the latest scored_at ({last_scored}): {len(later)} "
          f"({', '.join(f'{k}={v}' for k, v in Counter(origin(p) for p in later).items()) or 'none'})")

    # ---------------------------------------------------------------- 3. pools
    r.h("3. Per-role pools from the Apify files")
    by_slug = {p["slug"]: pid for pid, p in people.items() if p["slug"]}
    by_lid = {p["linkedin_id"]: pid for pid, p in people.items() if p["linkedin_id"]}
    pools: dict[str, set] = defaultdict(set)
    for folder in ("apify_pool", "apify_probe"):
        for f in sorted((root / "data" / folder).glob("*.json")):
            try:
                data = json.loads(f.read_text(encoding="utf-8", errors="replace"))
            except (OSError, json.JSONDecodeError) as e:
                r(f"  {folder}/{f.name}: unreadable ({type(e).__name__})")
                continue
            items = data.get("items") if isinstance(data, dict) else data
            items = items if isinstance(items, list) else []
            matched, unmatched = set(), 0
            for it in items:
                if not isinstance(it, dict):
                    continue
                pid = by_slug.get(slug_of(it.get("linkedinUrl"))) or \
                    by_slug.get((it.get("publicIdentifier") or "").lower()) or by_lid.get(it.get("id"))
                if pid is None:
                    unmatched += 1
                else:
                    matched.add(pid)
            if folder == "apify_pool":
                key = re.sub(r"_page\d+$", "", f.stem)
            else:
                key = f"probe:{f.stem}"
            pools[key] |= matched
            note = ""
            if isinstance(data, dict) and not items:
                err = data.get("error") or data.get("message") or (data.get("run") or {}).get("status")
                note = f"  status/error: {str(err)[:120]}" if err else ""
            r(f"  {folder}/{f.name}: items {len(items)}, matched to people {len(matched)}, "
              f"unmatched {unmatched}{note}")

    r("")
    r("  pool (distinct people) and their verdicts FOR THE POOL'S OWN ROLE")
    verdict_of = {(role, pid): v for role, pid, v, *_ in rows}
    probe_role = {"probe:role_1": "credit-controller", "probe:role_2": "client-manager",
                  "probe:role_3": "semi-senior", "probe:role_4": "client-manager"}
    for key in sorted(pools):
        role = key if key in scored else probe_role.get(key)
        s = pools[key]
        line = f"  {key:28} {len(s):4} people"
        if role:
            line += f" | as {role}: {vc(Counter(verdict_of.get((role, p), 'unscored') for p in s))}"
        r(line)
    in_any_pool = set().union(*pools.values()) if pools else set()
    li_people = [p for p in people if origin(p) in ("linkedin", "both")]
    r(f"  LinkedIn people in no Apify file: {sum(1 for p in li_people if p not in in_any_pool)}")
    multi = Counter(sum(1 for k in pools if not k.startswith("probe:") and p in pools[k]) for p in in_any_pool)
    r("  people by number of role pools (excluding probes) they appear in: " +
      ", ".join(f"{k}={v}" for k, v in sorted(multi.items())))

    r("")
    r("  ALL strong/possible per role, by where the person came from")
    for role in sorted(scored):
        for v in ("strong", "possible"):
            who = [pid for (ro, pid), vv in verdict_of.items() if ro == role and vv == v]
            src = Counter()
            for pid in who:
                if pid in pools.get(role, set()):
                    src["own role pool"] += 1
                elif origin(pid) in ("team_page", "both") and pid not in in_any_pool:
                    src["team page"] += 1
                elif pid in in_any_pool:
                    src["another role's pool or a probe"] += 1
                else:
                    src["other"] += 1
            r(f"  {role:18} {v:8} {len(who):3}  " + ", ".join(f"{k}={n}" for k, n in src.items()))

    # ---------------------------------------------------------------- 4. CC downgrade
    r.h("4. Credit-controller employer downgrade (23/09) vs stored verdicts")
    current = defaultdict(list)
    for pid, company in con.execute("SELECT person_id, company FROM positions WHERE is_current = 1"):
        if company:
            current[pid].append(company)

    def employer_hits(pid: int) -> bool:
        names = list(current.get(pid, []))
        hl = people.get(pid, {}).get("headline") or ""
        if " at " in hl:
            names.append(hl.split(" at ", 1)[1])
        return any(CC_DOWNGRADE.search(n) for n in names)

    cc = [(pid, v, reasons or "") for role, pid, v, _, reasons, _ in rows if role == "credit-controller"]
    hit = [(pid, v, rs) for pid, v, rs in cc if employer_hits(pid)]
    r(f"  credit-controller rows whose current employer matches the downgrade pattern: {len(hit)}")
    r(f"    their verdicts: {vc(Counter(v for _, v, _ in hit))}")
    r(f"    of those, reasons text mentions collections/finance-house: "
      f"{sum(1 for *_, rs in hit if re.search(r'collections|finance.house', rs, re.I))}")
    r(f"  strong credit controllers whose employer matches the pattern: "
      f"{sum(1 for _, v, _ in hit if v == 'strong')} of {sum(1 for _, v, _ in cc if v == 'strong')}")

    # ---------------------------------------------------------------- 5. richness
    r.h("5. What there is to build a profile from, by origin")
    npos = Counter(pid for (pid,) in con.execute("SELECT person_id FROM positions"))
    ncur = Counter(pid for (pid,) in con.execute("SELECT person_id FROM positions WHERE is_current = 1"))
    nq = Counter(pid for (pid,) in con.execute("SELECT person_id FROM qualifications"))
    for o in ("linkedin", "team_page", "both"):
        sub = [p for p in people if origin(p) == o]
        if not sub:
            continue
        r(f"  {o} ({len(sub)} people)")
        r(f"    headline present {sum(1 for p in sub if people[p]['headline'])}, "
          f"location_text present {sum(1 for p in sub if people[p]['loc'])}")
        r(f"    with >=1 position {sum(1 for p in sub if npos[p])}, with a current position "
          f"{sum(1 for p in sub if ncur[p])}, positions per person median {median([npos[p] for p in sub])} "
          f"max {max(npos[p] for p in sub)}")
        r(f"    with >=1 qualification row {sum(1 for p in sub if nq[p])}")
    r("")
    r("  qualifications: code x status")
    for code, status, n in con.execute("SELECT code, status, COUNT(*) FROM qualifications "
                                       "GROUP BY 1, 2 ORDER BY 1, 3 DESC"):
        r(f"    {str(code):18} {str(status):14} {n}")
    r("  qualifications: basis")
    for basis, n in con.execute("SELECT basis, COUNT(*) FROM qualifications GROUP BY 1 ORDER BY 2 DESC"):
        r(f"    {str(basis):30} {n}")
    r("")
    r("  source_records raw_json length by source")
    lens = defaultdict(list)
    for src, ln in con.execute("SELECT source, LENGTH(raw_json) FROM source_records"):
        lens[src].append(ln or 0)
    for src, ls in lens.items():
        r(f"    {src}: {len(ls)} records, median {median(ls)} chars, max {max(ls)}")

    exp = root / "data" / "exports" / "shortlist.html"
    if exp.exists():
        import datetime as _dt
        r("")
        r(f"  data/exports/shortlist.html last modified "
          f"{_dt.datetime.fromtimestamp(exp.stat().st_mtime).isoformat(timespec='seconds')} (local time)")


if __name__ == "__main__":
    sys.exit(main())

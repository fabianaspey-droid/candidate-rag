"""
src/make_label_sample.py  -  the hand-label answer key, drawn BEFORE any retrieval output is read.

Writes data/labels.csv: 20 candidates per role in scope, for Fabian to label from the profile text
alone. sourcing-core's verdicts are NOT in the sheet, so they can't anchor the labels.

Sampling: from each role's hard-filtered pool (not client staff; in the role's area or
"United Kingdom" only), stratified by sourcing-core's verdict so the sample holds strong, possible
and rejected people, then shuffled so the groups can't be told apart. Fixed seed, so it is
reproducible. Refuses to overwrite an existing labels.csv (it would destroy the labelling work)
unless --force is given.

Usage (from the candidate-rag folder, venv active):
    python src/make_label_sample.py
"""

from __future__ import annotations

import argparse
import csv
import json
import random
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DATA  # noqa: E402

PROFILES = DATA / "profiles.jsonl"
OUT = DATA / "labels.csv"
SEED = 20261008

# role -> (eval label key, location flag, how many to draw per sourcing-core verdict)
PLAN = {
    "credit-controller": ("credit_controller", "loc_area_core", {"strong": 7, "possible": 6, "reject": 7}),
    "semi-senior": ("semi_senior", "loc_area_wide", {"strong": 5, "possible": 8, "reject": 7}),
}


def in_pool(meta: dict, area_flag: str) -> bool:
    return (not meta["client_staff"]) and (meta[area_flag] or meta["loc_uk_only"])


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--force", action="store_true", help="overwrite an existing labels.csv")
    args = ap.parse_args()
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    if OUT.exists() and not args.force:
        print(f"ERROR: {OUT} already exists. Refusing to overwrite your labels. Use --force only if you mean it.")
        return 2

    profiles = [json.loads(l) for l in PROFILES.open(encoding="utf-8") if l.strip()]
    rng = random.Random(SEED)
    rows = []
    print("=" * 70)
    print("LABEL SAMPLE  (safe to paste: counts only)")
    print("=" * 70)
    for role, (key, area, quota) in PLAN.items():
        pool = [p for p in profiles if in_pool(p["metadata"], area)]
        drawn = []
        for verdict, n in quota.items():
            group = [p for p in pool if p["metadata"][f"eval_label_{key}"] == verdict]
            take = rng.sample(group, min(n, len(group)))
            drawn += take
            print(f"{role}: {verdict:8} drew {len(take)} of {len(group)} in the pool")
        rng.shuffle(drawn)
        for p in drawn:
            rows.append({"role": role, "id": p["id"], "profile_text": p["text"],
                         "your_label": "", "notes": ""})
        print(f"{role}: {len(drawn)} to label (pool after hard filters: {len(pool)})")

    OUT.parent.mkdir(parents=True, exist_ok=True)
    with OUT.open("w", encoding="utf-8-sig", newline="") as f:  # utf-8-sig so Excel reads it correctly
        w = csv.DictWriter(f, fieldnames=["role", "id", "profile_text", "your_label", "notes"])
        w.writeheader()
        w.writerows(rows)
    print(f"written: {OUT}  ({len(rows)} rows)")
    return 0


if __name__ == "__main__":
    sys.exit(main())

"""
src/evaluate.py  -  Step 6 of candidate-rag: measure retrieval and re-ranking, write RESULTS.md.

Reads (no API calls, no cost):
  data/labels.csv                 the answer key: 20 per role, labelled strong / possible / no
                                  (labelled by an LLM reviewer blind to sourcing-core and retrieval)
  data/retrieval_<role>.json      Step 4, four methods
  data/rerank_<role>.json         Step 5, run 1 (and rerank_<role>_run2.json if it exists)
  data/profiles.jsonl             sourcing-core's rule verdicts (eval_* keys), job titles

Measures, per role:
  1. Retrieval: of the answer-key strong candidates, how many each method put in its top 20
     (recall at 20), against what a random 20 would get. Also the same against the rules' strong.
  2. Re-ranking: of the candidates Claude marked strong, how many the answer key agrees are strong,
     and how many it says no to (false positives). Only candidates inside the labelled sample can
     be judged; the rest are counted as not judged.
  3. Rules versus the RAG pipeline: who each found that the other did not.
  4. Run-to-run variation, if a second re-rank run exists.
  5. Cost per run.

Counts, not percentages: with about 20 labelled candidates per role, percentages would suggest a
larger sample than there is.

Usage (from the candidate-rag folder, venv active):
    python src/evaluate.py
"""

from __future__ import annotations

import csv
import json
import sys
from collections import Counter
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from paths import DATA, OUT, RESULTS, SAMPLE  # noqa: E402
ROLES = {"credit-controller": "credit_controller", "semi-senior": "semi_senior"}
METHODS = ["sections_rrf", "whole_ad", "keyword_bm25", "hybrid_rrf"]
RERANKED = "hybrid_rrf"


def load_labels() -> dict[str, dict[str, str]]:
    raw = (DATA / "labels.csv").read_bytes()
    try:
        text = raw.decode("utf-8-sig")
    except UnicodeDecodeError:  # Excel sometimes re-saves as Windows-1252
        text = raw.decode("cp1252")
    out: dict[str, dict[str, str]] = {r: {} for r in ROLES}
    for row in csv.DictReader(text.splitlines()):
        role, pid, lab = row.get("role", ""), row.get("id", ""), (row.get("your_label") or "").strip().lower()
        if role in out and pid and lab:
            out[role][pid] = lab
    return out


def load_json(name: str):
    p = OUT / name
    return json.loads(p.read_text(encoding="utf-8")) if p.exists() else None


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    labels = load_labels()
    meta = {json.loads(l)["id"]: json.loads(l)["metadata"]
            for l in (DATA / "profiles.jsonl").open(encoding="utf-8") if l.strip()}

    md = ["# Results" + (" (INVENTED SAMPLE DATA)" if SAMPLE else ""), "",
          ("Run on the invented sample in sample/. These numbers say nothing about real performance; "
           "they only show the pipeline running end to end." if SAMPLE else
           "Candidate matching over a real pool of 145 LinkedIn profiles sourced for two live accountancy-practice "
           "roles (credit controller; semi-senior accountant). All figures are counts."), "",
          "**How to read these numbers**", "",
          "- The answer key is 20 candidates per role, sampled at random within each rule-based verdict group "
          "and labelled strong / possible / no by an LLM reviewer (Claude, in the build session) from the profile "
          "text alone, before any retrieval output existed and without seeing the rule-based verdicts (it had "
          "seen 4 of the 40 in earlier check output). It is not a human-labelled set. The rule-based shortlist "
          "it is compared with was reviewed by the recruiter before the original demo.",
          "- The re-ranker is also Claude (Haiku 5.5), so agreement between the two is partly circular.",
          "- Hybrid search was added after the first retrieval run showed the embedding model missing "
          "qualified semi-seniors, and was chosen as the input to re-ranking after seeing all four methods.",
          "- Small samples: a difference of one or two candidates is noise.", ""]

    print("=" * 70)
    print("STEP 6 EVALUATION  (safe to paste: ids, job titles and counts only)")
    print("=" * 70)
    for role, key in ROLES.items():
        lab = labels[role]
        retr = load_json(f"retrieval_{role}.json")
        rr1 = load_json(f"rerank_{role}.json")
        rr2 = load_json(f"rerank_{role}_run2.json")
        if not retr:
            print(f"{role}: no retrieval output; run retrieve.py first. Skipped.")
            continue
        n_pool = retr["pool_size"]
        key_strong = {p for p, v in lab.items() if v == "strong"}
        key_possible = {p for p, v in lab.items() if v == "possible"}
        key_no = {p for p, v in lab.items() if v == "no"}
        rules = {p: m[f"eval_label_{key}"] for p, m in meta.items()}
        rules_strong = {p for p, v in rules.items() if v == "strong"}
        title = lambda p: meta[p]["current_title"] or "(no current role)"  # noqa: E731

        print("")
        print(f"### {role}")
        print(f"answer key: {len(lab)} labelled (strong {len(key_strong)}, possible {len(key_possible)}, "
              f"no {len(key_no)}); pool {n_pool}")
        md += [f"## {role.replace('-', ' ').capitalize()}", "",
               f"Pool after hard filters: {n_pool}. Answer key: {len(lab)} labelled "
               f"({len(key_strong)} strong, {len(key_possible)} possible, {len(key_no)} no). "
               f"Rule-based strong in the pool: {len(rules_strong & set(_pool_ids(retr, meta)))}.", "",
               "### Retrieval: top 20 per method", "",
               "| Method | Answer-key strong found | Answer-key 'no' let in | Rule-based strong found |",
               "|---|---|---|---|"]
        rand_key = 20 * len(key_strong) / n_pool
        rand_rules = 20 * len(rules_strong & set(_pool_ids(retr, meta))) / n_pool
        md.append(f"| Random 20 (expected) | {rand_key:.1f} of {len(key_strong)} | - | "
                  f"{rand_rules:.1f} of {len(rules_strong & set(_pool_ids(retr, meta)))} |")
        print(f"retrieval, top 20 (random 20 would find {rand_key:.1f} of {len(key_strong)} key-strong):")
        for m in METHODS:
            top = {r["id"] for r in retr["methods"].get(m, [])}
            a, f_, rs = len(top & key_strong), len(top & key_no), len(top & rules_strong)
            print(f"  {m:13} key-strong {a} of {len(key_strong)}; key-no let in {f_}; rules-strong {rs}")
            md.append(f"| {m}{' (fed to re-ranking)' if m == RERANKED else ''} | {a} of {len(key_strong)} | {f_} | "
                      f"{rs} of {len(rules_strong & set(_pool_ids(retr, meta)))} |")
        top_rr = {r["id"] for r in retr["methods"][RERANKED]}
        missed = sorted(key_strong - top_rr, key=int)
        print(f"  key-strong missed by {RERANKED}: " + (", ".join(f"{p} ({title(p)})" for p in missed) or "none"))
        if not rr1:
            print("  re-ranking: not run yet (python src/rerank.py --spend)")
            md += ["", "Re-ranking not run yet.", ""]
            continue

        # ------------------------------------------------------------- re-ranking
        def judge(run):
            cands = {c["id"]: c for c in run["candidates"]}
            strong = {p for p, c in cands.items() if c["tier"] == "strong"}
            judged = strong & set(lab)
            return cands, strong, judged

        cands, c_strong, judged = judge(rr1)
        agree = judged & key_strong
        fp = judged & key_no
        soft = judged & key_possible
        key_strong_in_retrieved = key_strong & top_rr
        recovered = {p for p in key_strong_in_retrieved if cands.get(p, {}).get("tier") == "strong"}
        u = rr1["usage"]
        print(f"re-ranking (run 1, {rr1['model']}, prompt {rr1.get('prompt_version', 'v1')}): Claude strong {len(c_strong)}; inside the answer key {len(judged)}: "
              f"agreed strong {len(agree)}, key says possible {len(soft)}, key says no (false positive) {len(fp)}; "
              f"not judged {len(c_strong - judged)}")
        print(f"  key-strong that reached Claude: {len(key_strong_in_retrieved)}; Claude kept strong: {len(recovered)}; "
              f"Claude's tiers for the rest: " + ", ".join(
                  f"{p} {cands[p]['tier']}" for p in sorted(key_strong_in_retrieved - recovered, key=int)))
        print(f"  evidence verified {sum(c['evidence_verified'] for c in rr1['candidates'])} of {len(rr1['candidates'])}; "
              f"downgraded for unverified evidence {len(rr1['guards']['downgraded_unverified'])}; "
              f"cost ${u['cost_usd']:.4f} ({u['input_tokens']} in, {u['output_tokens']} out)")
        md += ["", f"### Re-ranking ({rr1['model']}, prompt {rr1.get('prompt_version', 'v1')}, the {RERANKED} top 20)", "",
               "| Measure | Count |", "|---|---|",
               f"| Marked strong by Claude | {len(c_strong)} |",
               f"| ...of which in the answer key | {len(judged)} |",
               f"| ...answer key agrees strong (precision) | {len(agree)} of {len(judged)} |",
               f"| ...answer key says possible | {len(soft)} |",
               f"| ...answer key says no (false positives) | {len(fp)} |",
               f"| Answer-key strong that reached Claude | {len(key_strong_in_retrieved)} |",
               f"| ...kept strong by Claude | {len(recovered)} |",
               f"| Evidence quotes verified word for word | {sum(c['evidence_verified'] for c in rr1['candidates'])} "
               f"of {len(rr1['candidates'])} |",
               f"| Downgraded by the evidence guard | {len(rr1['guards']['downgraded_unverified'])} |",
               f"| Cost of the run | ${u['cost_usd']:.4f} ({u['input_tokens']} tokens in, {u['output_tokens']} out) |"]

        # ------------------------------------------------------------- rules vs RAG
        rag_only = sorted(c_strong - rules_strong, key=int)
        rules_only = sorted((rules_strong & set(_pool_ids(retr, meta))) - c_strong, key=int)
        print(f"  Claude strong, rules not strong: " + (", ".join(
            f"{p} ({title(p)}; rules {rules[p]}; key {lab.get(p, 'unlabelled')})" for p in rag_only) or "none"))
        why = lambda p: "not retrieved" if p not in top_rr else f"Claude {cands[p]['tier']}"  # noqa: E731
        shown = rules_only[:15]
        print(f"  rules strong, Claude not strong: {len(rules_only)}" + "".join(
            f"\n    {p} ({title(p)}; {why(p)}; key {lab.get(p, 'unlabelled')})" for p in shown)
            + ("\n    ..." if len(rules_only) > 15 else ""))
        md += ["", "### Where the pipeline and the rules disagree", "",
               f"- Claude strong, rules not strong: {len(rag_only)} (answer key: "
               f"{sum(1 for p in rag_only if lab.get(p) == 'strong')} strong, "
               f"{sum(1 for p in rag_only if lab.get(p) == 'no')} no, "
               f"{sum(1 for p in rag_only if p not in lab)} not in the sample)",
               f"- Rules strong, Claude not strong: {len(rules_only)} "
               f"(not retrieved: {sum(1 for p in rules_only if p not in top_rr)}; "
               f"retrieved but Claude possible: {sum(1 for p in rules_only if p in top_rr and cands[p]['tier'] == 'possible')}; "
               f"retrieved but Claude reject: {sum(1 for p in rules_only if p in top_rr and cands[p]['tier'] == 'reject')})"]

        # ------------------------------------------------------------- run 2
        if rr2:
            c2, s2, j2 = judge(rr2)
            common = set(cands) & set(c2)
            tier_changes = sum(1 for p in common if cands[p]["tier"] != c2[p]["tier"])
            diffs = [abs(cands[p]["score"] - c2[p]["score"]) for p in common]
            agree2 = j2 & key_strong
            fp2 = j2 & key_no
            print(f"  run 2: Claude strong {len(s2)} (run 1: {len(c_strong)}); same strong set: {s2 == c_strong}; "
                  f"tier changed for {tier_changes} of {len(common)}; mean score change {sum(diffs)/len(diffs):.1f}, "
                  f"max {max(diffs)}; agreed strong {len(agree2)} of {len(j2)}; false positives {len(fp2)}; "
                  f"cost ${rr2['usage']['cost_usd']:.4f}")
            md += ["", "### Run-to-run variation (re-ranking run twice on the same 20)", "",
                   "| | Run 1 | Run 2 |", "|---|---|---|",
                   f"| Marked strong | {len(c_strong)} | {len(s2)} |",
                   f"| Answer key agrees strong | {len(agree)} of {len(judged)} | {len(agree2)} of {len(j2)} |",
                   f"| False positives | {len(fp)} | {len(fp2)} |",
                   f"| Cost | ${u['cost_usd']:.4f} | ${rr2['usage']['cost_usd']:.4f} |", "",
                   f"Tier changed for {tier_changes} of {len(common)} candidates between runs; "
                   f"score changed by {sum(diffs)/len(diffs):.1f} points on average (largest {max(diffs)})."]
        else:
            print("  run 2: not run yet")

        # ------------------------------------------------------------- Step 7 agent
        ag = load_json(f"agent_{role}.json")
        tr = load_json(f"agent_trace_{role}.json")
        if ag:
            a_c = {c["id"]: c for c in ag["candidates"]}
            a_strong = {p for p, c in a_c.items() if c["tier"] == "strong"}
            a_j = a_strong & set(lab)
            searches = [e for e in (tr or {}).get("trace", []) if e.get("tool") == "search_candidates"]
            seen = set((tr or {}).get("seen_ids", []))
            au = ag["usage"]
            print(f"agent: {au['turns']} turns, {len(searches)} searches, {len(seen)} candidates seen; strong {len(a_strong)}; "
                  f"in answer key {len(a_j)}: agreed {len(a_j & key_strong)}, false positives {len(a_j & key_no)}; "
                  f"key-strong it saw {len(key_strong & seen)} of {len(key_strong)}; "
                  f"strong overlap with re-ranking {len(a_strong & c_strong)}; ${au['cost_usd']:.4f}")
            md += ["", "### Step 7: the agent (Claude chooses its own searches)", "",
                   "| Measure | Agent | Re-ranking (run 1) |", "|---|---|---|",
                   f"| Searches made | {len(searches)} | 4 fixed section queries + BM25 |",
                   f"| Candidates seen | {len(seen)} | 20 |",
                   f"| Answer-key strong seen | {len(key_strong & seen)} of {len(key_strong)} | "
                   f"{len(key_strong_in_retrieved)} of {len(key_strong)} |",
                   f"| Marked strong | {len(a_strong)} | {len(c_strong)} |",
                   f"| Answer key agrees strong | {len(a_j & key_strong)} of {len(a_j)} | {len(agree)} of {len(judged)} |",
                   f"| False positives | {len(a_j & key_no)} | {len(fp)} |",
                   f"| Cost | ${au['cost_usd']:.4f} ({au['turns']} turns) | ${u['cost_usd']:.4f} |", "",
                   "Searches the agent chose: " + "; ".join(
                       f"\"{e['query']}\"" + (f" (filters {e['filters']})" if e.get("filters") else "") for e in searches)]
        md.append("")

    # ------------------------------------------------------------- rules vs answer key (whole sample)
    md += ["## How the rule-based verdicts compare with the answer key", "",
           "| Role | Rules strong / key strong | Rules strong / key no | Rules reject / key strong |", "|---|---|---|---|"]
    for role, key in ROLES.items():
        lab = labels[role]
        pairs = Counter((meta[p][f"eval_label_{key}"], v) for p, v in lab.items())
        md.append(f"| {role} | {pairs[('strong', 'strong')]} | {pairs[('strong', 'no')]} | {pairs[('reject', 'strong')]} |")
        print(f"rules vs answer key, {role}: " + ", ".join(f"{a}->{b} {n}" for (a, b), n in sorted(pairs.items())))
    RESULTS.write_text("\n".join(md) + "\n", encoding="utf-8")
    print(f"\nwritten: {RESULTS}")
    return 0


def _pool_ids(retr: dict, meta: dict) -> list[str]:
    """The role's filtered pool, rebuilt from the same rule retrieve.py uses."""
    flag = "loc_area_core" if retr["role"] == "credit-controller" else "loc_area_wide"
    return [p for p, m in meta.items() if not m["client_staff"] and (m[flag] or m["loc_uk_only"])]


if __name__ == "__main__":
    sys.exit(main())

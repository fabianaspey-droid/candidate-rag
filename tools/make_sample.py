"""
tools/make_sample.py  -  builds the INVENTED sample in sample/ so anyone can run the pipeline.

Every person, employer and town here is made up. Profiles are rendered with the same build_text()
the real ingest uses, so the pipeline treats them exactly as it treats real data. The sourcing-core
style rule verdicts (eval_label_*) and the answer key (sample/labels.csv) are invented too.

Usage:  python tools/make_sample.py
"""
import csv
import json
import sys
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from ingest import build_text  # noqa: E402

CORE = "Haverleigh, England, United Kingdom"
WIDE = "Marlow Cross, England, United Kingdom"
FAR = "Penhallow, Cornwall, United Kingdom"
UK = "United Kingdom"

# The sample's hiring firm is Ashby Pell (named in sample/jds), so its staff, and only its staff, are client_staff.
# Fixed 14/10: the first version flagged a different firm's employee and left two Ashby Pell staff unflagged.
# id, location, current (title, employer, start), previous [(title, employer, start, end)], quals [(code, status)],
# headline, rules verdict credit-controller, rules verdict semi-senior, client staff
PEOPLE = [
    (1, CORE, ("Credit Controller", "Brannock Hale Chartered Accountants", "Mar 2021"), [("Accounts Assistant", "Ferncliffe Foods", "Jan 2018", "Feb 2021")], [], "Credit Controller | Partner billing and WIP queries", "strong", "reject", False),
    (2, CORE, ("Credit Controller", "Brindle & Oakes LLP", "Jun 2022"), [("Credit Control Assistant", "Tamsin Logistics", "May 2019", "Jun 2022")], [("CICM", "studying")], "Credit controller in a law firm", "strong", "reject", False),
    (3, WIDE, ("Senior Credit Controller", "Greywell Engineering", "Feb 2020"), [("Credit Controller", "Orrin Packaging", "Jan 2015", "Jan 2020")], [], "Senior Credit Controller", "strong", "reject", False),
    (4, CORE, ("Credit Controller", "Kestrel Motor Finance", "Apr 2023"), [("Collections Agent", "Northgate Recoveries", "Jan 2020", "Mar 2023")], [], "Collections and arrears specialist", "possible", "reject", False),
    (5, CORE, ("Credit Control Manager", "Larkspur Distribution", "Sep 2016"), [("Credit Controller", "Larkspur Distribution", "May 2010", "Sep 2016")], [("CICM", "held")], "Credit Control Manager, team of six", "possible", "reject", False),
    (6, UK, ("Sales Ledger Clerk", "Pemberton Hardware", "Jan 2024"), [("Customer Service Advisor", "Bellhaven Energy", "Jun 2021", "Dec 2023")], [], "Sales ledger and customer service", "possible", "reject", False),
    (7, CORE, ("Accounts Receivable Assistant", "Quarrington Hotels", "Aug 2022"), [("Receptionist", "Quarrington Hotels", "Mar 2020", "Aug 2022")], [], "Accounts receivable, eager to grow in finance", "possible", "reject", False),
    (8, CORE, ("Credit Controller", "Ashby Pell Chartered Accountants", "Jan 2019"), [("Credit Controller", "Velmont Utilities", "Feb 2014", "Dec 2018")], [], "Credit controller at Ashby Pell", "strong", "reject", True),
    (9, FAR, ("Credit Controller", "Tregenna Builders Merchants", "Oct 2020"), [], [], "Credit Controller", "reject", "reject", False),
    (10, CORE, ("Semi Senior Accountant", "Mallory Crisp & Co", "Feb 2024"), [("Accounts Junior", "Mallory Crisp & Co", "Sep 2021", "Feb 2024")], [("AAT_QUALIFIED", "held"), ("ACCA", "studying")], "Semi-senior, ACCA student", "reject", "strong", False),
    (11, WIDE, ("Accounts and Audit Semi Senior", "Fenwick Lyle LLP", "May 2023"), [("Audit Junior", "Fenwick Lyle LLP", "Sep 2021", "May 2023")], [("AAT_QUALIFIED", "held")], "Accounts and audit semi-senior", "reject", "strong", False),
    (12, CORE, ("Trainee Chartered Accountant", "Ormsby Vane Accountants", "Sep 2024"), [("Sales Assistant", "Halcyon Books", "Jun 2022", "Aug 2024")], [("ACA", "studying")], "ACA trainee", "reject", "possible", False),
    (13, CORE, ("Accounts Senior", "Pellow Gant Chartered Accountants", "Mar 2022"), [("Semi Senior", "Pellow Gant Chartered Accountants", "Apr 2020", "Mar 2022")], [("ACCA", "part_qualified")], "Accounts senior, ACCA part-qualified", "reject", "strong", False),
    (14, CORE, ("Audit Manager", "Rowan Teague LLP", "Jan 2015"), [("Audit Senior", "Rowan Teague LLP", "Sep 2010", "Dec 2014")], [("ACA", "held")], "Audit Manager", "reject", "reject", False),
    (15, CORE, ("Assistant Accountant", "Silverdale Plastics", "Jun 2023"), [("Purchase Ledger Clerk", "Silverdale Plastics", "Jan 2021", "Jun 2023")], [("AAT_UNSPEC", "studying")], "Assistant accountant in industry", "reject", "possible", False),
    (16, WIDE, ("Semi Senior Accountant", "Hartwell Bligh Accountants", "Nov 2021"), [], [], "Semi Senior Accountant", "reject", "possible", False),
    (17, CORE, ("Bookkeeper", "Thistle Lane Bookkeeping", "Feb 2019"), [("Office Administrator", "Corrie Estates", "Mar 2015", "Jan 2019")], [("AAT_L3", "held")], "Bookkeeper, Xero and QuickBooks", "reject", "possible", False),
    (18, CORE, ("Tax Semi Senior", "Corwen Tilt Accountants", "Aug 2023"), [("Tax Trainee", "Corwen Tilt Accountants", "Sep 2021", "Aug 2023")], [("ATT", "held")], "Personal tax semi-senior", "reject", "possible", False),
    (19, CORE, ("Warehouse Supervisor", "Dunmore Freight", "Apr 2018"), [], [], "Warehouse supervisor", "reject", "reject", False),
    (20, CORE, ("Marketing Executive", "Bramble Digital", "Jan 2022"), [], [], "Digital marketing", "reject", "reject", False),
    (21, UK, ("Accounts Semi Senior", "Coldridge & Moss", "Jul 2023"), [("Accounts Assistant", "Coldridge & Moss", "Jul 2021", "Jul 2023")], [("AAT_QUALIFIED", "likely_held")], "Accounts semi-senior in practice", "reject", "possible", False),
    (22, CORE, ("Finance Assistant", "Haverleigh District Council", "May 2020"), [], [], "Finance assistant, public sector", "reject", "reject", False),
]
LABELS = {  # invented answer key
    "credit-controller": {"1": "strong", "2": "strong", "3": "strong", "4": "possible", "5": "possible",
                          "6": "possible", "7": "possible", "10": "no", "19": "no", "20": "no"},
    "semi-senior": {"10": "strong", "11": "strong", "13": "strong", "12": "possible", "16": "possible",
                    "17": "possible", "21": "strong", "14": "no", "1": "no", "20": "no"},
}


def main() -> None:
    out = PROJECT / "sample" / "profiles.jsonl"
    with out.open("w", encoding="utf-8") as f:
        for pid, loc, cur, prev, quals, headline, v_cc, v_ss, staff in PEOPLE:
            positions = [{"seq": 0, "title": cur[0], "company": cur[1], "start": cur[2], "end": "", "is_current": True}]
            positions += [{"seq": i + 1, "title": t, "company": c, "start": s, "end": e, "is_current": False}
                          for i, (t, c, s, e) in enumerate(prev)]
            q = [{"code": c, "status": s} for c, s in quals]
            text, _ = build_text({"headline": headline, "location": loc}, positions, q)
            codes = {c for c, _ in quals}
            meta = {
                "person_id": pid, "source": "linkedin", "location_text": loc,
                "loc_area_core": loc == CORE, "loc_area_wide": loc in (CORE, WIDE), "loc_uk_only": loc == UK,
                "current_title": cur[0], "current_company": cur[1], "qual_codes": "|".join(sorted(codes)),
                "has_any_qual": bool(quals),
                "aat_qualified": any(c == "AAT_QUALIFIED" and s in ("held", "likely_held") for c, s in quals),
                "aca_acca_held": any(c in ("ACA", "ACCA") and s == "held" for c, s in quals),
                "aca_acca_in_progress": any(c in ("ACA", "ACCA") and s in ("studying", "part_qualified", "finalist")
                                            for c, s in quals),
                "cicm": "CICM" in codes, "client_staff": staff, "n_words": len(text.split()),
                "eval_label_credit_controller": v_cc, "eval_score_credit_controller": -1,
                "eval_label_semi_senior": v_ss, "eval_score_semi_senior": -1,
                "eval_label_client_manager": "reject", "eval_score_client_manager": -1,
            }
            f.write(json.dumps({"id": str(pid), "text": text, "metadata": meta}) + "\n")
    texts = {str(p[0]): None for p in PEOPLE}
    with (PROJECT / "sample" / "labels.csv").open("w", encoding="utf-8-sig", newline="") as f:
        w = csv.DictWriter(f, fieldnames=["role", "id", "profile_text", "your_label", "notes"])
        w.writeheader()
        for role, labs in LABELS.items():
            for pid, lab in labs.items():
                assert pid in texts
                w.writerow({"role": role, "id": pid, "profile_text": "", "your_label": lab, "notes": "invented"})
    print(f"wrote {len(PEOPLE)} invented profiles and {sum(len(v) for v in LABELS.values())} invented labels")


if __name__ == "__main__":
    main()

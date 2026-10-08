"""
tools/check_public.py  -  run before every push. Scans every file git would publish for real data.

Looks for:
  * the private terms in data/client.json (client and agency names, real place names);
  * every real candidate's full name and every real employer, read from sourcing-core READ-ONLY
    (database copied to a temp folder, copy opened mode=ro);
  * LinkedIn profile URLs and email addresses.

Files checked = what `git ls-files --cached --others --exclude-standard` lists, i.e. exactly what
`git add .` would stage. Prints file:line and the kind of hit; person names are never printed.
Exit code 1 if anything is found.

Usage (from the candidate-rag folder, venv active):
    python tools/check_public.py
"""
import json
import re
import sqlite3
import subprocess
import sys
import tempfile
from pathlib import Path

PROJECT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(PROJECT / "src"))
from ingest import DEFAULT_ROOT, open_copy  # noqa: E402

GENERIC = {"self-employed", "self employed", "freelance", "career break", "various", "confidential",
           "travel", "volunteer", "contract", "consultant", "retired", "student", "university"}
MIN_LEN = 5


def tracked_files() -> list[Path]:
    out = subprocess.run(["git", "ls-files", "--cached", "--others", "--exclude-standard"],
                         cwd=PROJECT, capture_output=True, text=True, check=True).stdout
    return [PROJECT / line for line in out.splitlines() if line.strip()]


def main() -> int:
    if hasattr(sys.stdout, "reconfigure"):
        sys.stdout.reconfigure(encoding="utf-8", errors="replace")
    cfg_path = PROJECT / "data" / "client.json"
    private = json.loads(cfg_path.read_text(encoding="utf-8")).get("private_terms", []) if cfg_path.exists() else []
    if not private:
        print("WARNING: no private_terms in data/client.json; client names are not being checked.")

    names, employers = set(), set()
    db = DEFAULT_ROOT / "data" / "core.sqlite"
    if db.is_file():
        with tempfile.TemporaryDirectory(ignore_cleanup_errors=True) as tmp:
            con = open_copy(db, Path(tmp))
            try:
                names = {" ".join(n.split()) for (n,) in con.execute("SELECT full_name FROM people") if n and " " in n.strip()}
                employers = {" ".join(c.split()) for (c,) in con.execute("SELECT DISTINCT company FROM positions") if c}
            finally:
                con.close()
    else:
        print(f"WARNING: sourcing-core not found at {db}; real names and employers are not being checked.")
    employers = {e for e in employers if len(e) >= MIN_LEN and e.lower() not in GENERIC}

    # Spaces, underscores and hyphens are treated as the same separator, so a two-word private term
    # also catches its underscore, hyphen and run-together forms (on 08/10 the agency's name, written as
    # a Python file name with an underscore, slipped past the first version of this check).
    def loose(term: str) -> re.Pattern:
        parts = [re.escape(p) for p in re.split(r"[\s_-]+", term.strip()) if p]
        return re.compile(r"(?<![A-Za-z0-9])" + r"[\s_-]*".join(parts) + r"(?![A-Za-z0-9])", re.I)

    patterns = [("private term", t, loose(t)) for t in private]
    patterns += [("real employer", e, re.compile(r"\b" + re.escape(e) + r"\b", re.I)) for e in sorted(employers)]
    patterns += [("real person name", None, re.compile(r"\b" + re.escape(n) + r"\b", re.I)) for n in sorted(names)]
    patterns += [("LinkedIn URL", None, re.compile(r"linkedin\.com/in/", re.I)),
                 ("email address", None, re.compile(r"[\w.+-]+@[\w-]+\.[\w.]+"))]

    files = tracked_files()
    hits = 0
    for f in files:
        if f.suffix.lower() in (".png", ".jpg", ".ico", ".pyc"):
            continue
        try:
            lines = f.read_text(encoding="utf-8", errors="replace").splitlines()
        except OSError:
            continue
        for i, line in enumerate(lines, 1):
            for kind, term, pat in patterns:
                if pat.search(line):
                    hits += 1
                    shown = f": {term}" if term else ""
                    print(f"HIT {f.relative_to(PROJECT)}:{i}  {kind}{shown}")
    print("=" * 70)
    print(f"checked {len(files)} files against {len(private)} private terms, {len(employers)} real employers, "
          f"{len(names)} real names, LinkedIn URLs and emails")
    print("CLEAN: nothing found" if not hits else f"FOUND {hits} hit(s). Fix them before pushing.")
    return 1 if hits else 0


if __name__ == "__main__":
    sys.exit(main())

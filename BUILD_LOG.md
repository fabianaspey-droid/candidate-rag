# candidate-rag build log

Candidate-matching RAG over a recruitment agency's candidate pool held by sourcing-core (a private
sourcing engine). For publication, the agency, its client and the client's towns are anonymised
throughout: "the agency", "the client", "core area" and "wide area".
sourcing-core is read-only throughout. Real candidate data stays out of git.

sourcing-core location: `C:\Users\Admin\Downloads\sourcing-core`
candidate-rag location: `C:\Users\Admin\Downloads\candidate-rag`

## Step 0: Inspect the source data (08/10/2026)

### inspect_source.py

Standard library only, takes `--root` pointing at sourcing-core.

**Read-only by design, and why it copies the database first:** the first version opened the
database directly with `mode=ro` and `PRAGMA query_only=ON`. Tested on a mock sourcing-core in WAL
mode, that still made SQLite create `core.sqlite-shm` and `core.sqlite-wal` beside the database.
The database bytes were unchanged, but that is still a write into sourcing-core's folder. Fixed by
copying the database (and any `-wal` file) to a temp folder that is deleted afterwards, and opening
the copy. Retested: sourcing-core's folder has the same files with the same bytes before and after,
and a row that existed only in a pending `-wal` file was still counted.

**Masking failure on the first real run (08/10):** the report was pasted into the build chat with
real identifiers in it. Two causes, both in inspect_source.py, not in sourcing-core:
1. Masking went by column name only. `contacts.value` holds LinkedIn URLs, but "value" doesn't look
   like an identifier column, so two URLs printed in full.
2. Files mentioning a role were printed if their path matched "config|client|role".
   `data\apify_probe\role_1.json` matched "role", so the first 150 lines of every raw Apify file in
   data\apify_probe and data\apify_pool printed: roughly one person's name, LinkedIn URL and
   headline per file, about a dozen people.
Fixed: values are now masked when they look like a URL, linkedin.com address, email, phone or
LinkedIn member id, whatever the column is called, and nothing under a `data` folder is ever
printed. Retested on a mock with both cases. Lesson carried forward: anything pasted from here on
prints counts only (inspect_pools.py was written that way).

### What the first real run showed

- One database: `data\core.sqlite` (14.9 MB).
- people 233. contacts 145, all `apify_linkedin` (LinkedIn URLs). person_sources 134, all
  `team_page`. positions 844, qualifications 90, source_records 195, firms 23,
  ch_companies 83,998 (Companies House), schema_meta 1.
- The labels are in the `matches` table: role_id, person_id, verdict, score, reasons, scored_at.
  435 rows = 145 per role, for all three roles:
  - credit-controller: strong 50, possible 12, reject 83
  - semi-senior: strong 5, possible 21, reject 119
  - client-manager: strong 1, possible 6, reject 138
- Roles are data in the client config file in sourcing-core (ROLES and POOL_SEARCHES): criteria
  only (title families, qualification rules, employer preferences, location town lists).
  **No job description text is stored anywhere.** The longest text in the database is
  `matches.reasons` (432 chars) apart from raw Apify JSON.
- Raw pool files: `data\apify_pool\` credit-controller pages 1-4, semi-senior pages 1-3,
  client-manager page 1 (143 lines, so probably the refused run). `data\apify_probe\` role_1-4.
- `data\exports\shortlist.html` exists; the matches table is the label source, not this file.

### Claims in the build guide that the database does not support

1. "Around 68 credit controllers, and 86 people for the semi-senior role." The database has no
   per-role pool: matches scores the same 145 people against every role. The per-role pool can
   only be rebuilt from the Apify files. Pending inspect_pools.py.
2. "86 including the firm-website channel." It is not yet known whether team-page people were
   scored at all (145 scored people = 145 LinkedIn contacts, which suggests not). Pending.
3. Credit-controller strong = 50 is the figure from the first full pull, before the 23/09
   employer downgrade (consumer and finance-house collections to possible) was proposed. Either
   the downgrade was never applied or matches were not rescored after it. Pending, to be checked
   from the data before proposing a cause.

### Open decisions

- **Retrieval scope (pushback on the guide's Step 4 role-pool filter):** because sourcing-core
  scores everyone against every role, filtering retrieval to "the role pool" would hand retrieval
  the answer. Proposed: search the whole scored pool for every role, with hard filters on
  location and qualification only. Decide after inspect_pools.py.
- **client-manager:** no pool of its own (fetch refused), 1 strong. Likely out of scope for the
  evaluation, since recall cannot be measured on one strong candidate. Decide after
  inspect_pools.py.
- **Job description text:** to be saved as plain text in `jds\` (credit-controller.txt,
  semi-senior.txt) from the original ads.

### inspect_pools.py (second pass, counts only)

Written to answer the three pending claims: people by origin; who matches covers and whether
anyone was added after scoring; per-role pools rebuilt from the Apify files with their verdicts;
the credit-controller downgrade against stored verdicts; and how much profile material exists per
origin. Tested on a mock with the real schema: no names, URLs or firm addresses in the output,
and the mock sourcing-core folder byte-identical afterwards.

### What inspect_pools.py showed on the real database (08/10)

- **233 people, two separate groups, no overlap:** 145 LinkedIn people (person_id 1-145) and
  88 team-page people (person_id 146-233).
- **matches covers the 145 LinkedIn people only,** each scored against all three roles in a single
  run at 2026-09-23T16:54:01Z. **The 88 team-page people were never scored:** all 88 were first seen
  after that run. shortlist.html is the export of that same run (modified 17:54 local = 16:54Z).
- **Per-role pools rebuilt from the Apify files** (every item matched to a person, none unmatched):
  - credit-controller pool: 68 people (pages 1-3; page 4 returned 0 items). As credit-controller:
    strong 50, possible 11, reject 7.
  - semi-senior pool: 56 people (pages 1-3). As semi-senior: strong 5, possible 20, reject 31.
  - client-manager pool: 0 people (page 1 returned 0 items).
  - The four probe files hold 10 people each; 21 of the 145 appear only in a probe.
  - Every rules-strong candidate for credit-controller (50) and semi-senior (5) is in that role's
    own pool. client-manager's 1 strong and 6 possible all came from other pools or probes.
- **Credit-controller employer downgrade IS applied:** the 6 people whose current employer matches
  the pattern are all possible, all 6 with reasons citing it, and 0 of the 50 strong match it.
- **Profile material:** LinkedIn people have a headline and location (145/145), a median of 5
  positions and qualification rows for 41. Team-page people have one position each, a location
  for only 5 of 88, and qualification rows for 25.
- Qualifications are coded (AAT_QUALIFIED, ACA, ACCA, CICM, CTA and so on) with a status of held,
  likely_held, studying, part_qualified or finalist, and a basis (team_page, education, headline,
  name, about, certification).

### The pending claims, resolved against the database

1. "Around 68 credit controllers": **confirmed** as the credit-controller LinkedIn pool (68).
2. "86 people for the semi-senior role including the firm-website channel": **failed.** The
   semi-senior LinkedIn pool is 56, and the firm-website (team-page) people were never scored for
   any role, so none of them has a sourcing-core label.
3. My own claim that strong = 50 predated the 23/09 credit-controller downgrade: **failed.** The
   downgrade is in the stored verdicts; 50 strong is after it.

### What this means for the evaluation (proposed, awaiting Fabian's decision)

- **Search all 145 scored people for every role, not the role's pool.** Inside the
  credit-controller pool, 50 of 68 are rules-strong, so a random 20 from the pool would include
  about 15 strong candidates and retrieval would have nothing to prove. Across all 145, a random 20
  would include about 7 of the 50 for credit-controller and under 1 of the 5 for semi-senior.
- **client-manager out of scope for the evaluation:** no pool of its own and 1 rules-strong
  candidate, so recall can't be measured.
- **The 88 team-page people left out of the evaluation index:** no sourcing-core labels to compare
  against and 83 of 88 have no location. They can come back later as "people the rules never saw".
- **Credit-controller rules are loose:** 50 of 68 strong. Recall at 20 against 50 rules-strong is
  capped at 20 of 50, so for this role the useful measures are how many of the top 20 are strong
  (by hand labels) and false positives. This is also where re-ranking has the most to add: the
  advert asks for partner-facing, professional-services credit control, which the title rule can't
  see.

### Job descriptions

No job description text is stored in sourcing-core or in shortlist.html (checked 08/10: the export
holds role titles and counts only). The full adverts were found in the 23/09 sourcing-core build
chat, where Fabian pasted all three, and saved as `jds\credit-controller.txt`, `jds\semi-senior.txt`
and `jds\client-manager.txt`. Text is as pasted, apart from the leading "1./2./3. About the job"
numbering and two line breaks lost in pasting in the credit-controller ad ("Harborough.As" and
"The RoleThis").

**Known issue for Step 4:** the adverts are 344 (credit-controller), 401 (semi-senior) and 433
(client-manager) words. The embedding model reads roughly the first 256 word pieces (about 190
words) and silently drops the rest, so embedding the whole advert as the query would lose most of
"What we're looking for". To be decided at Step 4.

### Decisions (Fabian, 08/10, "Yes for all 3")

1. Retrieval searches all 145 scored LinkedIn people for every role. No role-pool filter.
2. client-manager is out of scope for the evaluation (no pool of its own, 1 rules-strong).
   `jds\client-manager.txt` is kept but not evaluated.
3. The 88 team-page people are left out of the evaluation index (never scored, 83 of 88 with no
   location).

Roles in scope for the evaluation: **credit-controller** and **semi-senior**.

jds files moved into `candidate-rag\jds\` and BUILD_LOG.md replaced on Fabian's machine, 08/10 14:55.

**Step 0 status:** everything identified is done: the database, role definitions, labels, pools and
job descriptions are located and recorded, and the scope is decided. **Not exercised:** nothing
has read the jds files yet, and the query-length issue above is open until Step 4.

## Step 1: Set up the project (08/10/2026)

Instructions given: git init, .gitignore before anything else, venv, install chromadb, anthropic,
python-dotenv and pytest with exact versions in requirements.txt, ANTHROPIC_API_KEY in .env,
folders src/, tests/, data/, README.md placeholder.

Python 3.12.10, git 2.54.0.windows.1, `git init` done 08/10.

**.gitignore instruction failed as given:** it was a PowerShell here-string (`@' ... '@`) inside an
indented code block. Pasted, every line carried leading spaces, so PowerShell never saw the
closing `'@` and sat at the `>>` prompt; nothing was written. Even if it had run, git would not
have matched "   .venv/" with its leading spaces. Replaced with a one-line `Set-Content` taking an
array, one pattern per line. No here-strings in instructions from here on.

**Held back: no commit in Step 1.** Anything committed now goes public in Step 8 unless history is
rewritten. The jds files and this log named the agency and the client, so whether the
public repository may name the client and its client has to be decided before the first commit.

**Check results (08/10, Fabian's machine):**
- `git check-ignore -v`: `.env` ignored by `.gitignore:2:.env`; both `data\inspect_*_output.txt`
  ignored by `.gitignore:3:data/`.
- `git status --porcelain --untracked-files=all`: .gitignore, BUILD_LOG.md, README.md,
  inspect_pools.py, inspect_source.py, jds/ (3 files), requirements.txt. Nothing from .env, data/
  or .venv/.
- requirements.txt: anthropic==1.12.1, chromadb==1.5.9, pytest==9.1.1, python-dotenv==1.2.4.
- Imports run from `C:\Users\Admin\Downloads\candidate-rag\.venv`.
- `.env` holds ANTHROPIC_API_KEY: starts `sk-ant-`, 108 characters. Key not printed.

**Step 1 status:** everything identified is done. **Not exercised:** the API key has only been
checked for format, not used; no API call has been made. No commit yet (held, see above).

## Step 2: Build the profile documents (08/10/2026)

**Built:** `src\ingest.py`. Reads sourcing-core through a temp copy (same read-only method as
Step 0) and writes `data\profiles.jsonl`, one record per candidate, id = sourcing-core person_id.

**Scope:** the 145 LinkedIn people sourcing-core scored (decision 3 above).

**Text, in this order:** current role (title, employer, dates); LinkedIn headline; qualifications
as stated with status (for example "ACCA (studying)", "AAT qualified"); location; "Source:
LinkedIn"; previous roles (up to 4, most recent first). Capped at 150 words. Previous roles are
dropped from the end first if a profile is over the cap.

**Deviation from the guide's order:** the guide lists previous roles before qualifications. In
testing, four long previous roles pushed qualifications to around word 100, near where the
embedding model stops reading. Previous roles moved to the end so that only old roles are ever
cut. Names are not in the text: they add nothing to matching.

**Metadata:** person_id, source, location_text; loc_area_core and loc_area_wide (the
town lists copied from the client config, not imported, so no sourcing-core code runs);
loc_uk_only; current_title, current_company; qual_codes; has_any_qual, aat_qualified (held or
likely held), aca_acca_held, aca_acca_in_progress (studying, part-qualified, finalist), cicm;
client_staff (current employer or headline matches the client's name); n_words; and, for evaluation only,
eval_label_* and eval_score_* for all three roles.

**Leak check:** reports two numbers. Label words (strong, possible, reject) outside the headline
must be 0: there they could only come from the template. Label words inside a LinkedIn headline
("strong communicator") are the person's own wording, reported for checking by eye. Tested by
planting "Possible hire" in a job title on a mock: caught as a leak.

**Ordering assumption checked by the script:** positions are ordered current first, then by seq
ascending, assuming seq 0 is the most recent (Apify's order). The check counts people whose current
role is not their lowest seq.

**Tested on a mock with the real schema (145 LinkedIn + 88 team-page):** 145 records, no
duplicates, team-page people excluded, client-name headline flagged as client staff, mock
sourcing-core byte-identical afterwards.

**First real run (08/10 16:31):** 145 records, 0 duplicate ids. Words per text min 24, median
64, max 116 (cap 150, so the cap never bit). 176 previous roles dropped, all from the four-role
limit, none from the word cap. Leak check outside the headline: 0. Label word inside a LinkedIn
headline: 1 (id 4), to be checked by eye. No current role: 10. No qualifications: 104. Location:
core area 134, wide area 134 (the wide area's extra towns add nobody), "United Kingdom"
only 8, neither 3. Flags: AAT qualified 14, ACA/ACCA held 1, ACA/ACCA in progress 9, CICM 5.
Client staff: 4. Eval labels match sourcing-core exactly (credit-controller 50/12/83,
semi-senior 5/21/119, client-manager 1/6/138). The three sample records read sensibly: current
role first, qualifications and location before the previous roles.

**Ordering assumption broken for 3 people.** Their current role is not their lowest seq. No cause
proposed yet. Added `--explain-order`, which prints seq, is_current and dates (no titles or
employers) for those people, so the stored data can be read first. The effect is limited
either way: the current role always comes first in the text; only the order of previous roles can
be affected.

**--explain-order output (08/10 16:33):** ids 68, 70 and 129 each have an entry at seq 0 that is
not current and has no start or end date ("? - ?"). Their dated positions run newest first as
assumed (id 68 also has two current roles, seq 1 and seq 4). So the assumption holds for dated
positions; the flag came from one undated entry stored first. What that entry is was not looked
at (titles not printed). Fix, independent of what it is: undated previous roles are sorted after
dated ones (and so are the first to fall off the four-role limit), and the check now compares
against the lowest DATED seq. Tested on a mock reproducing the pattern: check 0, undated entry
after the dated roles.

**id 4 headline checked by Fabian:** the label word is "Building Strong Relationships" in the
person's own headline. Not a leak. It also showed a double full stop where a headline already
ends in one ("Relationships.."); trailing full stops are now stripped from headlines.

**For Step 4:** the 4 client staff are in the index. They must be excluded by a hard filter
(client_staff = false) for every role, since the client's own staff must never be put forward.

**Rerun with the fixes (08/10 16:38):** ordering check 0; every other line unchanged (145
records, 0 duplicates, leak check 0, words 24/64/116, same flags and eval labels).

**Step 2 status:** everything identified is done. **Not exercised:** real word-piece counts
(measured in Step 3).

## Step 3: Embed and index in ChromaDB (08/10/2026)

**Built:** `src\index.py`. Persistent Chroma in `chroma\`, collection `candidates`, cosine
distance set through `configuration={"hnsw": {"space": "cosine"}}` (the chromadb 1.x form), Chroma's
default embedding function (all-MiniLM-L6-v2 through ONNX, local, no cost). Upsert keyed on
person_id, in batches of 64.

**Decision: eval_* keys are not stored in Chroma.** They stay in data\profiles.jsonl for Step 6
only. Retrieval physically cannot filter or rank on sourcing-core's verdicts, which is a stronger
guarantee than "never used in retrieval".

**Checks the script prints:** collection count before and after; ids in Chroma equal ids in
profiles.jsonl; no eval_* keys stored; distance space read back from Chroma; embedding
dimensions; real word-piece counts for every profile and every jds file, from a separate,
untruncated copy of the model's own tokenizer.json; the two sanity queries from the guide, top 5,
with the sourcing-core label shown beside each result for checking only.

**Confirmed in chromadb 1.5.9's source:** the default model truncates at 256 word pieces including
[CLS] and [SEP] (`enable_truncation(max_length=256)` in onnx_mini_lm_l6_v2.py).

**Tested here (the model download is blocked from the build workspace, so with a word-hashing
stand-in for the model, chromadb 1.5.9):** run 1 count 0 to 145; run 2 count 145 to 145; ids
match; no eval_* keys; space reads back "cosine"; 384 dimensions. The word-piece path was tested
with a toy tokenizer saved where the real one will be: counts are untruncated (the jds files came
out over 256).

**Learned in testing:** Chroma rebuilds the embedding function from the collection's saved config
("default") rather than using the object passed in.

**Real runs (08/10 16:40):** model downloaded (79.3 MB). Run 1: count 0 to 145. Run 2: 145 to
145, identical output. Ids match, no eval_* keys, cosine, 384 dimensions.
Profile word pieces: min 43, median 112, max 179; 0 over the 256 window.
Job descriptions: client-manager 509, credit-controller 445, semi-senior 505 word pieces, so the
model reads only the first 256 of each (roughly half).

Sanity query "credit controller chasing aged debt": top 5 all credit controllers (four Credit
Controller, one Senior Credit Controller), distances 0.532-0.548, all rules-strong.
Sanity query "semi-senior accountant studying ACCA": top 5 all accounts/audit practice staff
(Audit and Accounts semi senior; Group Accountant Trainee; Accounts and Audit Senior; Semi Senior
Accountant; Audit & Accounts Senior), all with ACA or ACCA, distances 0.302-0.350; rules: 1 strong,
4 possible. Right kind of person for both. Why four of these are only "possible" under the rules is
a Step 6 question, not looked into yet.

**Step 3 status:** everything identified is done. **Not exercised:** anything with the real job
descriptions as queries (Step 4).

## Step 4: Retrieve (08/10/2026), decisions proposed before building

1. **Query text.** Every advert is 445-509 word pieces against a 256 window, so embedding the
   whole advert silently drops the second half, which is where the requirements are. Proposed:
   split each advert into its sections, drop the sections that sell the employer (about the
   firm, benefits, closing), embed each remaining section as its own query, and combine the
   rankings with reciprocal rank fusion. Also run the whole advert as-is, as a comparison row.
2. **Hard filters.** client_staff = false, and location in the role's area or "United Kingdom"
   only (as sourcing-core does). No hard qualification filter for semi-senior: it would leave
   about 24 people, so a top 20 would be nearly everyone and prove nothing, and it would drop the
   no-visible-qualification people Fabian decided on 23/09 to keep as possibles.
3. **Hand-label sample drawn now and labelled blind, before retrieval output is read.** The guide
   labels in Step 6, after the shortlists have been seen, which biases the answer key towards
   what retrieval found.

**Fabian, 08/10 16:42:** "yes to all 3, I'm not too bothered about the specifics, I just want to
add a RAG build to my CV." Taken as: keep explanations short, keep the checks.

**Built:**
- `src\make_label_sample.py` writes `data\labels.csv`: 20 per role from the hard-filtered pool,
  stratified by sourcing-core verdict (credit-controller 7 strong / 6 possible / 7 reject;
  semi-senior 5 / 8 / 7), shuffled, fixed seed 20261008. Columns: role, id, profile_text,
  your_label, notes. No sourcing-core verdict in the sheet. Refuses to overwrite an existing
  labels.csv without --force.
- `src\retrieve.py`. Three methods per role inside the same hard filters:
  sections_rrf (main: advert split into sections, employer-selling sections dropped, each kept
  section a query, reciprocal rank fusion with k = 60), whole_ad (whole advert as one query,
  truncated by the model), keyword_bm25 (BM25 over profile text, same kept sections as the query).
  Saves data\retrieval_<role>.json. The check counts rules-strong and rules-possible in each top
  20, the random expectation, overlap between methods, the rules-strong that sections_rrf missed
  (when there are 20 or fewer), and the sections_rrf top 20 with titles.

**How the adverts split (real jds files):** credit-controller keeps "The Role", "What you'll be
doing", "What we're looking for"; drops the intro (employer pitch) and "Why join [the client]".
Semi-senior keeps "What you'll be doing", "Where this role can take you", "What we're looking
for", "Nice to have"; drops the intro, "What's on offer" and the "Interested in joining" sign-off.
Bullets were running together ("practice Liaising directly"); each bullet now gets a full stop.

**Tested on the mock (stand-in embedder):** Chroma's where clause returned exactly the pool the
Python rule computes (87 and 116 on the mock); client staff excluded; three top 20s per role;
JSON written; label sheet 40 rows with no verdict columns; second run of the sampler refused.

**Not exercised:** the real index, real word-piece counts per section, and Fabian's labels.

### The answer key: labelled by an LLM, not by hand (08/10 16:48)

Fabian drew the sample (40 rows) and asked the build chat to do the labelling: "I do not have
time for that. Can you just do the labelling." Done by Claude in the build chat, not by a human.

**What this changes:** the guide's "hand-labelled" no longer applies. RESULTS.md and the README
must say the answer key came from an LLM reviewer, not a person. It is also partly circular:
Claude labels, and Claude Haiku re-ranks in Step 5. Different model, but the same family.

**Blindness:** the reviewer had not seen any retrieval output (retrieve.py had not been run). It
had already seen sourcing-core's verdict for 4 of the 40 in earlier check output (ids 104 and 101,
credit-controller strong; id 23, semi-senior strong; id 29, semi-senior possible), and nothing for
the other 36. The sheet itself carried no verdicts.

**Basis:** the adverts, plus Fabian's 23/09 rules: consumer and finance-house collections are
possible at best; credit manager / credit control manager titles are possible as potentially too
senior; cautious on unclear qualifications; no false positives over coverage. One-line reason per
row in the notes column.

**Result:** credit-controller strong 7, possible 7, no 6. Semi-senior strong 8, possible 5, no 7.
id 42 appears in both samples (no for credit-controller, strong for semi-senior).

Fabian, 08/10 16:52: "I already checked the candidates in the first place for the client demo, so
they were already hand picked and qualified." sourcing-core's shortlist had his own review before
the demo; RESULTS.md can say so alongside the LLM-labelled answer key.

### First real run of retrieve.py (08/10 16:52)

- Pool after hard filters: 138 of 145 for both roles (client staff out: 4).
- **"Chroma returned exactly the same pool: False" for both roles.** Not yet explained.
- Query word pieces, all inside 256: credit-controller 76 / 139 / 122; semi-senior 84 / 61 / 86 / 31.
- credit-controller (50 rules-strong in pool, random 20 would hold 7.2): sections_rrf 18 strong +
  2 possible; whole_ad 18 + 1; keyword_bm25 6 + 4. Overlap with sections_rrf: whole_ad 14,
  keyword 6. Top 20 is 19 Credit Controllers and 1 Credit control business partner.
- semi-senior (5 rules-strong, random 0.7): sections_rrf 2 strong + 13 possible; whole_ad 3 + 9;
  keyword_bm25 4 + 8. **sections_rrf missed ids 22, 26, 42**, all AAT-qualified practice
  semi-seniors and all labelled strong by the LLM reviewer too, so they are obvious misses. The
  guide says to fix this here, not leave it to Claude.

No cause proposed for either. Built `src\diagnose_retrieval.py` to query first: (A) the metadata
filter alone through col.get(where), exact, no vector search, against the Python rule; (B) whether
the vector search returns the whole filtered pool when asked for it; (C) for ids 22, 26, 42, the
filter flags and their rank in every section query, the whole advert and the fusion. Tested on the
mock: runs, all True there.

**Diagnostic on the real index (08/10 16:54):**
- A. The filter is right: Python rule 138, Chroma get(where) 138, no difference either way, both roles.
- B. The search returned the whole pool: asked for 138, every query (whole advert and each
  section) returned 138, none missing. An unfiltered query for all 145 returned 145.
- So the "False" in retrieve.py did not reproduce. Either the search is not repeatable between
  runs or something differs between the two scripts. Not yet known; retrieve.py now prints the
  returned count, the missing and extra ids, and whether the same query gives the same result twice.
- C. ids 22, 26, 42 are not lost: they rank 17th to 34th per section and 23rd, 25th and 24th after
  fusion, just outside the top 20, below trainees, a manager and an industry accountant. This is a
  ranking problem, not a filter or search problem. BM25 caught 4 of the 5 rules-strong where
  sections_rrf caught 2: the embedding model blurs qualification acronyms and seniority, which
  keyword matching handles exactly.

**Change: hybrid_rrf added (08/10).** Lexical plus semantic hybrid search: reciprocal rank fusion of
the sections_rrf ranking and the BM25 ranking, one vote each, k = 60. Added AFTER seeing the
semi-senior miss, which RESULTS.md must say. Nothing else tuned (section choice, k, weights left as
they were), because tuning against the same 5 rules-strong people would fit the answer key.
All four methods are kept and reported. The "missed" list and the printed top 20 now use hybrid_rrf.

### Second real run of retrieve.py (08/10 16:56)

- Pool mismatch reproduced and pinned down: the whole-advert search asked for 138 and returned
  137; **id 137 missing, identical on repeat**; nothing extra. The diagnostic's run of the same query
  had returned 138. The filter is right (diagnostic A), so the vector search is dropping a person.
- chromadb 1.5.9's HNSW config, read in the build workspace: ef_search defaults to 100, below the
  138 being asked for. HNSW is approximate. **Hypothesis, not yet confirmed:** search depth below
  the pool size lets the graph search miss a node. id 137 has 13 years as a credit controller, so
  it matters.
- credit-controller: sections_rrf 18 rules-strong, whole_ad 18, keyword_bm25 6, **hybrid_rrf 14**.
  BM25 pulls in a cards governance manager (91), a regional credit lead (102) and a semi-senior
  accountant (47).
- semi-senior: sections_rrf 2 of 5, whole_ad 3, keyword_bm25 4, **hybrid_rrf 4 of 5** (misses
  id 22 only).
- No single method wins both roles. Hybrid trades some credit-controller precision for
  semi-senior recall.

**Fix (source, index.py): ef_search = 500**, above the collection size. Set on creation and, for a
collection that already exists, through col.modify (tested: an existing collection created without
it is upgraded to 500, and the setting persists after reopening). The check prints ef_search. The
experiment that confirms or kills the hypothesis: rerun index.py, then retrieve.py, and see whether
"Chroma returned exactly the same pool" turns True and id 137 comes back.

**Decision for Step 5: re-rank the hybrid_rrf top 20 for both roles.** Chosen after seeing the
results, which RESULTS.md must say. Reasoning: retrieval's job is recall, because a candidate missed
at retrieval can't be recovered by re-ranking, while the noise hybrid lets in (91, 102, 47) is
exactly what the re-ranker's precision rules should remove. One pipeline for both roles: hybrid
search, then Claude re-rank.

## Step 5: Re-rank with Claude (08/10/2026)

**Model change from the guide: Claude Haiku 5.5 (`claude-haiku-5-5`), not Haiku 4.5.** Checked on
platform.claude.com on 08/10: Haiku 5.5 is the current Haiku, $0.10 per million input tokens and
$0.50 per million output (prompts under 100k tokens); Haiku 4.5 is still listed at $1 and $5. Ten
times cheaper and newer. Haiku 4.5 stays available through `--model`.

**Built:** `src\rerank.py`. One call per role: the full advert (no 256 limit for Claude) and only
the 20 hybrid_rrf candidates. The system prompt carries the sourcing-core rules: no false positives
over coverage; cautious on qualifications; right title without a visible qualification is possible;
consumer and finance-house collections, and credit manager titles, possible at most for
credit-controller. Asks for JSON only: id, score 0-100, tier, reasoning, evidence quote.

**Guards:** reply must parse with the expected shape, or nothing is written for that role (the raw
reply is kept in data\rerank_raw_<role>.txt to look at); ids not in the retrieved set dropped;
duplicates dropped; malformed tier or score dropped; evidence checked against that candidate's own
text (only whitespace, case and curly quotes normalised; at least 15 characters); strong on an
unverified quote is downgraded to possible and flagged; retrieved candidates missing from the reply
logged. Output data\rerank_<role>.json with usage, real cost and the guard log.

**Cost control:** without --spend it is a dry run: input tokens counted with the free
count_tokens endpoint, output assumed at about 120 tokens per candidate and capped by
max_tokens = 8000 (any thinking counts as output). With --spend, real tokens and cost come from the
API response's usage.

**Tests: `tests\test_guards.py`, 9 tests, all passing in the build workspace** (anthropic 1.12.1,
pytest 9.1.1): verbatim evidence, normalisation limited to whitespace/case/quote marks, short quotes
unverified, unretrieved id dropped, strong-on-unverified downgraded, duplicate and missing logged,
malformed tier/score dropped, unparseable reply raises (so nothing is written), fenced JSON parses.

**Estimate before any spend (mock profiles, 4 characters per token):** about 3,000-4,000 input
tokens per role; worst case at the output cap about $0.004 per role, under 1 US cent for both. Based
on the mock, which predates the real profiles; the dry run on Fabian's machine replaces it with
counted figures.

**Not exercised:** any real API call; the ef_search fix on the real index.

### Real runs (08/10 17:15)

**ef_search fix confirmed.** index.py: 145 to 145, ef_search 500. retrieve.py: "Chroma returned
exactly the same pool: True" for both roles; asked for 138, returned 138, missing none, identical
on repeat. id 137 is back. The hypothesis (search depth below the pool size) held. Retrieval counts
are otherwise unchanged from 16:56.

**Tests:** 9 passed on Fabian's machine.

**Dry run:** input tokens counted by the API: credit-controller 5,966, semi-senior 6,124 (about
double the mock estimate, which used shorter fake profiles plus a character-based guess). Worst
case at the cap $0.0092 for both.

**Real run (--spend), claude-haiku-5-5:**
- credit-controller: 5,966 in, 6,006 out, $0.0036, 23.5 s, end_turn. 20 returned, 20 kept.
  Strong 2 (ids 2 and 103), possible 16, reject 2. No guard drops; 1 unverified quote (id 87, not
  strong, so no downgrade). Evidence verified 19 of 20.
- semi-senior: 6,124 in, 6,737 out, $0.0040, 26.0 s, end_turn. 20 returned, 20 kept. Strong 5
  (42, 27, 59, 45, 57), possible 14, reject 1. 1 unverified quote (id 23, not strong). Evidence
  verified 19 of 20.
- **Total $0.0076.** Output was about double the assumed 3,000 per role: the model's own reasoning
  counts as output. Still under the cap.
- Observations, not yet explained: Claude was far stricter than the rules on credit-controller
  (2 strong against 14 rules-strong in the same 20); id 59 (Tax and Accounts Consultant) is
  Claude-strong for semi-senior but rules-reject.

**Step 5 status:** everything identified is done: output parses, every id from the retrieved set,
every quote checked (2 unverified, flagged, neither strong), cost logged. **Not exercised:** the
guide's "read one shortlist yourself": Fabian has not read data\rerank_*.json.

## Step 6: Evaluate (08/10/2026)

**Built:** `src\evaluate.py`, no API calls. Per role: recall at 20 against the answer key for all
four retrieval methods and a random 20; answer-key "no" let in; the rules' strong found; Claude's
strong tier judged against the answer key (precision, false positives, and how many fall outside
the labelled sample and can't be judged); answer-key strong that reached Claude and whether Claude
kept them strong; both directions of rules-versus-pipeline disagreement with the reason (not
retrieved, Claude possible, Claude reject); run 2 comparison if present; cost. Also rules versus
answer key across the 40. Writes RESULTS.md with the caveats stated at the top (LLM-labelled answer
key, circularity, hybrid added and chosen after seeing results, small samples). Counts only.

`rerank.py` gained `--run N` so a second run writes rerank_<role>_run2.json beside run 1.

**Tested on the mock** (random labels and random re-rank tiers): all tables produced, run 2 section
appears when the file exists and is skipped when it doesn't.

**Not exercised:** the real data.

### Real results with prompt v1 (08/10 17:19)

Re-rank run 2: credit-controller $0.0037 (strong 2, 103, 89), semi-senior $0.0032 (strong 27, 42, 57,
59, 45; evidence 20 of 20). Total $0.0069. Runs 1 and 2 together: $0.0145.

**Retrieval (recall at 20 against the answer key):**
- credit-controller (7 key-strong; random 20 would find 1.0): sections_rrf 5, whole_ad 5,
  keyword_bm25 3, hybrid_rrf 5. Missed by hybrid: 106, 116 (both Credit Controllers). No method let in
  an answer-key "no".
- semi-senior (8 key-strong; random 1.2): sections_rrf 3, whole_ad 4, keyword_bm25 5,
  **hybrid_rrf 6**. Missed by hybrid: 22, 46.

**Re-ranking, prompt v1:**
- semi-senior: Claude strong 5; 3 in the answer key, **all 3 agreed, 0 false positives**; 2 not judged
  (45, 59). Of the 6 key-strong that reached Claude, it kept 3 strong (23, 26, 29 went to possible).
  Claude found id 27 (Accounts Senior; AAT qualified, studying ACA and CTA) strong where the rules said
  possible and the answer key says strong. Run 2: same strong set; 1 tier change of 20; mean score
  change 5.2.
- credit-controller: Claude strong 2 (ids 2 and 103), neither in the answer key, so precision can't be
  judged. **Of the 5 key-strong that reached Claude, it kept 0 strong** (3, 7, 89, 101, 104 all
  possible). Run 2: 3 strong (89 added); 1 tier change; mean score change 3.0.

**Failed claim, named:** I wrote that rerank.py's system prompt "carries the sourcing-core rules". It
did not, for credit-controller. sourcing-core's rule (the client config) is employers "preferred:
practice, law_firm; accepted: industry", with points for preferred. The v1 prompt said professional
services was "the best fit" and never said an industry credit controller could be strong, so Claude
treated a preference as a requirement and marked ordinary B2B credit controllers possible. A prompt
that misstated the rule, so fixed at the source rather than tuned.

**Fix: prompt v2.** States that the advert asks for credit control experience; a current or recent
B2B credit controller can be strong in any industry; professional services is a preference that
should score highest, not a requirement for strong. Collections and manager rules unchanged.
Semi-senior rules unchanged. PROMPT_VERSION recorded in each rerank output. Guard tests still
9 passed.

**Also observed:** the answer key tracks the rules closely. credit-controller: 19 of 20 identical
(strong to strong 7, possible to possible 6, reject to no 6; one reject to possible). Semi-senior:
3 rules-possible are key-strong; the rest identical. The LLM reviewer knew the 23/09 rules, so for
credit-controller "agrees with the answer key" and "agrees with the rules" are nearly the same
measure. RESULTS.md must say so.

**Plan:** move the v1 outputs (rerank_*, RESULTS.md) to data\archive_prompt_v1, rerun re-ranking
twice with v2 (about $0.015 based on the two v1 runs), rerun evaluate.

### Results with prompt v2 (08/10 17:23)

Retrieval unchanged (same index, same queries).

- **credit-controller, run 1:** Claude strong 12; 4 inside the answer key, **all 4 agreed, 0 false
  positives**; 8 not judged. Of the 5 key-strong that reached Claude, **4 kept strong** (v1: 0); 101
  possible. 1 strong downgraded by the evidence guard (unverified quote), the first time the guard
  changed an outcome. $0.0038 (6,048 in, 6,368 out). Run 2: 13 strong, 5 of 5 judged agreed, 0 false
  positives, tier changed for 5 of 20, mean score change 5.8 (max 20), $0.0034.
- **semi-senior, run 1:** Claude strong 5; 4 in the answer key, all 4 agreed, 0 false positives; 1 not
  judged. Kept 4 of 6 key-strong strong (26, 29 possible). $0.0037. Run 2: 5 strong (different set),
  3 of 3 agreed, 0 false positives, tier changed for 2 of 20, mean score change 5.7, $0.0035.
- **Pipeline versus rules:** credit-controller: Claude strong where the rules said reject: 102
  (Regional Credit Lead), unlabelled. "Lead" is not caught by the manager rule; a possible false
  positive that can't be judged. Semi-senior: 27 (rules possible, key strong) and 59 (Tax and Accounts
  Consultant, rules reject, unlabelled).
- **Rules strong, pipeline not strong:** credit-controller 39, mostly "not retrieved". The rules
  call 50 of 138 strong, and a top 20 can't hold 50; this is the retrieval cap, not a miss.
- **Cost of v2:** $0.0075 + $0.0069. All re-ranking runs today, v1 and v2: about $0.03.

**Step 6 status:** everything identified is done; RESULTS.md written by evaluate.py with prompt v2.
**Not exercised:** a human reading the shortlists; any answer-key check of the 8 + 1 Claude-strong
candidates outside the labelled sample.

## Step 7: Agentic search with tool use (08/10/2026)

Fabian: "let's do step 7".

**Built:** `src\agent.py`. Tools through the Messages API: search_candidates (query, n_results 1-20,
optional filters on aat_qualified, aca_acca_held, aca_acca_in_progress, cicm, has_any_qual),
get_profile (full text by id), submit_shortlist (ends the run). The hard filters (not client staff;
role's area or "United Kingdom") are always added by the code: Claude can narrow, never widen; any
other filter key it sends is ignored. get_profile refuses an id no search has returned. System prompt =
the Step 5 v2 rules plus instructions to search from several angles (title synonyms, qualifications,
adjacent roles), read profiles when snippets are not enough, and submit once. Parallel tool calls in
one turn are handled.

**Limits:** 8 turns; 120,000 cumulative input tokens per role, checked after each turn (so one turn can
overshoot); 8,000 output tokens per call. If a limit is hit: no shortlist written, trace saved and
marked aborted.

**Guards:** the Step 5 apply_guards code, with "retrieved" = every id a search returned in this run.
"Missing from reply" is not counted as a fault for the agent, since it submits what it assessed.

**Trace:** data\agent_trace_<role>.json: every API call (tokens, stop reason) and every tool call
(turn, query, filters actually applied, n_results, ids returned; get_profile id and any refusal).

**evaluate.py:** adds a Step 7 table per role when agent output exists: searches, candidates seen,
answer-key strong seen, strong, precision, false positives, cost, the queries the agent chose; side by
side with Step 5 run 1.

**Tests:** `tests\test_agent.py`, 2 tests with a scripted fake Claude and a fake collection: a full
session (parallel searches; a client_staff filter from Claude ignored; get_profile refused for an
unseen id; shortlist with an invented id dropped and an unverified strong downgraded); and the turn
limit writing no shortlist. 11 of 11 tests pass in the build workspace.

**Cost before the first run:** ceiling per role about $0.046 (120k + 20k input, 8 turns x 8,000 output
on Haiku 5.5), about $0.09 for both. Expected well under that: the guide's own shape (about 6 turns,
50-60k input per role) is about $0.006 input, plus output at about the 6,000 per call measured in
Step 5 if every turn reasons at length, so about 1.5-2 cents per role, 3-4 cents for both. The output
figure is from Step 5 calls, which predate the agent.

**Not exercised:** a real agent run.

### Real agent run (08/10 17:27)

- **credit-controller:** submitted in 5 turns; 102,970 in, 11,347 out; **$0.0160**; 47.5 s. 7 searches,
  genuinely different angles: professional-services credit control with partners/billing/WIP; aged
  debt and chasing; accounts receivable / sales ledger / debtors; first-finance-role accounts
  assistants; accountancy-firm credit control; CICM credit management; AAT/ACCA-qualified debtor
  management. 32 get_profile calls, none refused; 63 distinct candidates seen. Submitted 38: strong 17,
  possible 16, reject 5. Evidence 38 of 38 verified; no guard drops.
- **semi-senior:** submitted in 5 turns; 79,995 in, 7,140 out; **$0.0116**; 29.6 s. 7 searches,
  including three that used the qualification filters (aat_qualified 14 results, aca_acca_in_progress
  8, aca_acca_held 1) and a bookkeeping/software angle. 25 get_profile calls, none refused; 40 seen.
  Submitted 34: strong 7, possible 18, reject 9. Evidence 34 of 34.
- **Total $0.0275,** inside the 3-4 cent estimate and far under the $0.09 ceiling. Input ran close to the
  120k budget on credit-controller (102,970), because every turn resends the whole conversation.

**Against the answer key (evaluate.py):**
- credit-controller: saw 6 of 7 key-strong (hybrid top 20: 5). Strong 17; 3 in the answer key, all 3
  agreed, 0 false positives; 14 not judged. Strong overlap with Step 5 run 1: 6.
- semi-senior: **saw 8 of 8 key-strong** (hybrid top 20: 6), including id 22, which hybrid_rrf and
  sections_rrf missed. **Failed claim, named:** I told Fabian id 22 was missed by "every fixed retrieval
  method"; the data only shows hybrid and sections_rrf missing it. Whether whole_ad or BM25 had it was
  never checked. Strong 7; 5 in the answer key, **all 5 agreed, 0 false positives**. Overlap
  with Step 5: 5.

**Step 7 status:** everything identified is done: the trace shows different searches, limits never hit,
guards ran, results compared in RESULTS.md. **Not exercised:** the abort path on real calls (tested
only with the fake client); a second agent run to measure its run-to-run variation.

## Step 8: Publish to GitHub (08/10/2026)

**Decisions (Fabian, 08/10 17:30):** anonymise ("defo anonymise"); public repository named candidate-rag.

**Anonymisation:**
- The client's town lists and name pattern moved out of ingest.py into `data\client.json`
  (git-ignored). ingest.py stops with an error if it is missing.
- Location fields renamed to neutral names: loc_area_core (was the main office's area) and
  loc_area_wide (core plus the second office's extra towns). The values are unchanged.
- The client's name became "client staff" in code and output; retrieve.py drops any "About ..." section
  rather than naming the firm.
- The real adverts in `jds\` are git-ignored (`/jds/`, anchored so sample/jds is still published).
- This log: agency, client and town names replaced, as listed at the top.
- RESULTS.md no longer lists job titles, only counts.

**Sample mode:** `src\paths.py` switches every script between real data (data\, chroma\, jds\, all
ignored) and the invented sample (sample\ inputs committed; sample\out\ and sample\chroma\ ignored) with
`--sample`. `tools\make_sample.py` builds 22 invented candidates with the same build_text() the real
ingest uses, invented rule verdicts and an invented 20-row answer key; two invented adverts for a
fictional practice. Tested here end to end (index, retrieve, evaluate) with the stand-in embedder.

**Tests:** `tests\test_index.py` adds the double-run upsert check (in-memory Chroma, tiny stand-in
embedder) and the Chroma-safe metadata check. tests now use sample\jds. 13 of 13 pass here.

**Leak check:** `tools\check_public.py` scans every file `git add .` would stage for the private terms in
data\client.json, every real full name and every real employer in sourcing-core (read through a temp
copy), LinkedIn URLs and email addresses. Exit code 1 on any hit. Must be CLEAN before the push.

**Spend to date:** re-rank v1 $0.0076 + $0.0069, v2 $0.0075 + $0.0069, agent $0.0275: about $0.051.

**Not exercised:** the renamed fields on the real data (needs ingest, index, retrieve, evaluate rerun;
no API cost), the leak check against the real names and employers, and the push itself.

### Checks before the push (08/10 17:41)

- Real data rebuilt with the neutral field names (ingest, index, retrieve, evaluate): every count
  identical to the 17:23 and 17:27 runs (core area 134, wide area 134; pools 138; retrieval, re-ranking
  and agent figures unchanged). The renaming changed names only, not behaviour.
- Tests: 13 passed on Fabian's machine.
- Sample mode end to end (index, retrieve, evaluate with --sample): 22 profiles, pools 17 and 20, model
  reads both invented adverts whole (226 and 231 word pieces). Known cosmetic issues in sample output
  only: the "random 20" expectation is not capped when the pool is under 20, and invented candidate 3
  is labelled strong for credit-controller while sitting outside its core area, so the filter
  correctly excludes them.
- Leak check: **CLEAN**: 25 files checked against 6 private terms, 465 real employers, 232 real names,
  LinkedIn URLs and emails.
- git status lists 25 files: .gitignore, BUILD_LOG.md, README.md, RESULTS.md, inspect_pools.py,
  inspect_source.py, requirements.txt, sample (4 files), src (10 files), tests (3), tools (2). Nothing
  from data\, jds\, chroma\, sample\out\ or sample\chroma\.

**Failed claim, named: "Leak check: CLEAN" was wrong.** Straight after it, a plain search of the build
log found the agency's name three times, written as a Python file name with an underscore (the client
config file in sourcing-core). check_public.py matched private terms with spaces only, so the
underscore form passed. Fixed at the source: separators (space, underscore, hyphen, none) are now
treated alike for private terms; tested on the underscore, hyphen, spaced and run-together forms, and
on a near miss ("unbalanced") that must not match. The three lines in this log were rewritten. The
check must be rerun and be CLEAN again before the push.

### Published (08/10 17:50)

Leak check rerun with the widened matching: CLEAN. First commit failed silently (git had no user name
or email), so the first push said "src refspec main does not match any"; identity set, committed,
pushed. **Public at https://github.com/fabianaspey-droid/candidate-rag**, 1 commit. Checked from
outside: public, README renders, and none of the client, agency or town names appear on the page.

**Step 8 status:** everything identified is done. **Not exercised:** a fresh clone run by someone else
on another machine (the sample run was done in the working folder, not a clean clone); the Claude
stages in sample mode.

# Part 2: MCP server (from 13/10/2026)

Build guide: "Candidate RAG MCP server: build guide", 13 Oct 2026. Same working rules as Part 1, plus:
check the current official MCP Python SDK documentation before any MCP code; the server is read-only;
real data never in the repo; the public repo only ever runs on the sample.

## Step 0: Inspect the repo (13/10)

Read from the published repo (github.com/fabianaspey-droid/candidate-rag, commit 02de40b), cloned
and compared file by file with the build workspace: identical apart from line endings in
sample/labels.csv and the "Published" note at the end of this log, which was not pushed.

**Where each MCP piece's logic lived:**
- list_roles: `ROLES` in retrieve.py (role id, advert file, location field). No one-line summary
  exists; to be added in Step 2.
- search_candidates: semantic search existed only as a method inside the agent loop
  (`agent.Session.search`); hybrid search existed only inline in `retrieve.main()`, built for whole
  adverts, not a free-text query. The pieces were plain functions in retrieve.py (`embed_rank`, `bm25`,
  `rrf`, `chroma_where`).
- get_profile: `agent.Session.get_profile` (inside the agent loop); text from `index.load_profiles()`.
- jd://{role}: the advert files under `paths.JDS`, named by `ROLES[role]["jd"]`.
- Opening the index: the same lines repeated in retrieve.py, agent.py and diagnose_retrieval.py.
- Sample or real: `paths.py` (`--sample` or `CANDIDATE_RAG_SAMPLE=1` switches DATA, OUT, CHROMA_DIR, JDS).

**Refactor (the guide's "first job"): `src\search.py`.** Moved from retrieve.py unchanged: STOPWORDS,
chroma_where, python_filter, embed_rank, rrf, tokens, bm25, RRF_K (retrieve.py re-exports them under
the old names, so diagnose_retrieval.py and other imports still work). Moved from agent.py: FILTER_FLAGS,
SNIPPET_CHARS, and the filter-building in Session.search as `build_where()`. New: `hybrid_rank()`
(semantic over the whole filtered pool fused with BM25 over the same pool, one vote each, the same
fusion as retrieve.py's hybrid_rrf, for a free-text query) and `open_collection()` (raises
`IndexNotReady` with the command to run if the index folder, the collection or its contents are missing).

**Decision: the Step 7 agent keeps plain semantic search.** Moving it to hybrid would change its
behaviour and invalidate the published agent results. Hybrid is a separate shared function for the
MCP server.

**Checks (build workspace, stand-in embedder):** sample retrieval output saved before the refactor and
compared after: both retrieval_*.json files byte-identical. 13 of 13 tests pass. Every module imports.
open_collection on a missing folder raises "No index at ... Run: python src/index.py".

**Push-back for later steps, recorded now:**
1. Rule 5 (real data never leaves the machine) conflicts with Step 6.4 (point Claude at the real
   data). Through the Claude desktop app, the real candidates' profiles would go to Anthropic as
   conversation content under a personal account. These are third parties' personal data, sourced
   for a client demo. Recommendation: skip Step 6.4; the CV claim needs only the sample. If it is done,
   check the account's privacy setting on using chats to improve models first.
2. "Read-only" for ChromaDB has to be measured, not assumed: opening a PersistentClient may write to
   chroma.sqlite3 even for queries (as SQLite did in Part 1, Step 0). Step 4 will hash the index files
   before and after a session of tool calls.

**Not exercised:** the refactor on Fabian's machine against the real index (real retrieval output to be
compared before and after).

**Refactor check on Fabian's machine (14/10):**
- First attempt proved nothing: the venv was not active, so retrieve.py failed on "No module named
  chromadb", never rewrote its outputs, and the comparison printed IDENTICAL by comparing the old files
  with themselves. Rerun with the outputs deleted first, so a failed run can't pass.
- Second attempt: real semi-senior, sample credit-controller and sample semi-senior IDENTICAL; **real
  credit-controller CHANGED**. 13 tests passed.
- Queried before proposing a cause: whole_ad and keyword_bm25 identical to the last digit; sections_rrf
  and hybrid_rrf differ. id 4's sections_rrf score moved by exactly 0.000158 = 1/79 - 1/80, i.e. one
  rank (80th to 79th) in one section's full-pool ranking, far below the top 20.
- Three more runs with no code change: run a differs from run b; b equals c. **Same code, different
  output, so the variation is in the search itself, not the refactor.** Cause not identified.
- Effect: hybrid_rrf top 20 has exactly the same members as before (order shifted), so the set sent
  to Claude in Steps 5 and 7 is unaffected. sections_rrf's top 20 swapped one member at the boundary
  (id 140 before, id 92 now). Recorded as observed behaviour; Step 4 tests to look at repeatability
  of the MCP search.

**Step 0 status:** everything identified is done. **Not exercised:** the cause of the run-to-run
variation.

## Step 1: Install the SDK (14/10)

Checked the official SDK before writing any MCP code (rule 2): PyPI lists **mcp 2.3.0** as the latest
release; the GitHub README (modelcontextprotocol/python-sdk) calls v2 the current stable line, "a
major rework" with breaking changes, v1.x on its own branch. Read from the installed 2.3.0 package
itself rather than memory:
- High-level server: `from mcp.server.mcpserver import MCPServer`. `FastMCP` is gone: importing
  `mcp.server.fastmcp` in 2.x raises ModuleNotFoundError pointing at the migration guide
  (py.sdk.modelcontextprotocol.io/v2/migration).
- Decorators: `@mcp.tool(name, title, description, annotations, ..., structured_output)`,
  `@mcp.resource(uri, *, name, title, description, mime_type, ...)` with URI templates,
  `@mcp.prompt(name, title, description)`.
- `mcp.run(transport="stdio")` (default), synchronous.
- Requires Python >=3.10. Installed beside chromadb 1.5.9 in the build workspace: pip check "No broken
  requirements found".

**Decision: mcp[cli]==2.3.0.** The README's advice to pin below 2 is for existing v1 code; this
project has none. The cli extra provides the `mcp dev` command for the Inspector in Step 5.

**Install on Fabian's machine (14/10 16:01):** mcp 2.3.0 and mcp-types 2.3.0 installed (plus pyjwt,
cryptography, cffi, pycparser, python-multipart, pywin32, sse-starlette, starlette). pip check: "No
broken requirements found". `from mcp.server.mcpserver import MCPServer` imports. 13 tests passed.
Sample retrieval still runs ("exactly the same pool: True" for both roles). requirements.txt now:
anthropic==1.12.1, chromadb==1.5.9, mcp[cli]==2.3.0, pytest==9.1.1, python-dotenv==1.2.4. The Step 0
comparison folders (data\pre_refactor, sample\pre_refactor) deleted; the sample one would otherwise
have been published.

**Step 1 status:** everything identified is done. **Not exercised:** whether the Claude desktop app
works with a 2.x server (Step 6).

## Step 2: Design the tools (14/10), PROPOSED, awaiting Fabian's agreement

**Data source:** the server defaults to the invented sample. Real data only when the environment
variable `CANDIDATE_RAG_REAL=1` is set for the server process. Note this is the opposite of the
command-line scripts, which default to real data and take --sample; for a server anyone can clone and
connect, sample-by-default is the safe way round.

**Rules that hold for every tool:** read-only; the client's own staff are never returned by any tool,
including get_profile; errors come back as MCP tool errors with a message saying what to do, never an
empty result that looks like "no matches"; every tool carries the MCP read-only annotation
(readOnlyHint) so a client can see it changes nothing.

### Tool: list_roles
- Parameters: none.
- Returns: `{"mode": "sample" | "real", "roles": [{"role": str, "title": str, "summary": str,
  "jd_uri": str}]}`
- Description: "List the roles this candidate pool can be searched against. Returns each role's id,
  title, a one-line summary and the URI of its full job description (jd://<role>). Call this first if
  you don't know the role ids: search_candidates and the shortlist_for_role prompt both need one."

### Tool: search_candidates
- Parameters: `role` (str, required, one of list_roles), `query` (str, required, plain words),
  `n_results` (int, 1-20, default 10), `filters` (optional object: aat_qualified, aca_acca_held,
  aca_acca_in_progress, cicm, has_any_qual, each true/false).
- Method: hybrid search (search.hybrid_rank): semantic ranking over the role's whole filtered pool
  fused with BM25 over the same pool.
- Returns: `{"role", "query", "filters_applied", "pool_size", "results": [{"rank": int, "id": str,
  "title": str, "employer": str, "location": str, "qualifications": [str], "snippet": str,
  "score": float}]}`. snippet = first 300 characters of the profile.
- Description: "Search the candidate pool for one role, by meaning and by keyword together (hybrid
  search). The role's hard filters are always applied: only candidates in that role's location area,
  or whose location says just 'United Kingdom', and never the client's own staff. You can narrow
  further with qualification filters. Returns up to n_results candidates (1 to 20, default 10), best
  first, each with id, current title, employer, location, qualification codes, the first 300
  characters of their profile and a relevance score that can only be compared within the same search.
  Snippets are not full profiles: call get_profile before judging or quoting anyone. Several short
  searches from different angles (job title synonyms, qualifications, related roles) find more than one
  long query."

### Tool: get_profile
- Parameters: `candidate_id` (str, required).
- Returns: `{"id", "title", "employer", "location", "qualifications": [str], "profile": str}`.
- Description: "Return one candidate's full profile by id, as given by search_candidates: current role,
  headline, qualifications as stated, location and previous roles. Read this before recommending a
  candidate, and quote only text that appears in it. Unknown ids return an error."

### Resource: jd://{role}
- Plain text, the full job advert for the role. Description: "The full job advert for a role. Read it
  before searching so your searches reflect what the advert asks for."

### Prompt: shortlist_for_role
- Arguments: `role` (str), `how_many` (int, default 3).
- Text: read jd://<role>; search from at least three different angles; read the full profile of every
  candidate before recommending them; apply the same cautious rules as the Step 5 re-ranker (prompt
  v2: no false positives over coverage; only stated qualifications count; right title without a
  visible qualification is possible, not strong; for credit control, consumer or finance-house
  collections and manager titles are possible at most); give each recommendation an evidence quote
  copied exactly from that profile; if fewer than how_many are strong, say so rather than padding.

### Decisions in this design (for agreement)
1. `role` is required on search_candidates, not optional. The location filter and the client-staff
   exclusion belong to the role; a search without one would either have no location rule or need a
   second, separate filter that could widen what the agent can see.
2. Location is not a free parameter: it is set by the role.
3. More than 20 results is rejected with an error rather than silently cut to 20, so a caller learns
   the limit.
4. No rerank_candidates tool: the connected assistant does that reasoning, and leaving it out keeps the
   server free to run, as the guide suggests.

**Fabian, 14/10 16:05: "I agree with all."** Step 2 design agreed as written above.

## Step 3: Build the server (14/10)

**Built: `src\mcp_server.py`** on mcp 2.3.0: `MCPServer("candidate-rag", instructions=...)`; tools
list_roles, search_candidates, get_profile (each with ToolAnnotations read_only_hint=True,
destructive_hint=False, idempotent_hint=True, open_world_hint=False); resource template `jd://{role}`
(text/plain); prompt shortlist_for_role(role, how_many=3) carrying the Step 5 v2 rules. Returns are
TypedDicts, so the SDK publishes an output schema and returns structured content. Search uses the
shared search.hybrid_rank. Profiles and the collection load once, on first use.

**Read from the SDK before writing (rule 2), and tried in a prototype server:** ToolError (in
mcp.server.mcpserver.exceptions) gives the assistant your message with is_error; any other exception
is treated as a crash and the assistant sees only "Error executing tool <name>". ResourceNotFoundError
for a missing resource. The SDK logs to stderr. Client(MCPServer) connects in-process;
Client(StdioServerParameters) launches the real server as a subprocess over stdio.

**Found and fixed while building:**
1. **Loose filter values.** The prototype accepted `{"cicm": "yes"}` as true: pydantic coerces
   loosely by default. Filters now use StrictBool, and unknown filter keys are rejected (extra="forbid").
2. **paths.py created a folder on import.** `OUT.mkdir(...)` ran whenever paths.py was imported, a write.
   It now skips that when CANDIDATE_RAG_READONLY=1, which the server sets before importing it.
3. **Chroma writes on every open, measured.** An in-process session of read-only tool calls changed
   sample\chroma\chroma.sqlite3 and an HNSW file (length.bin). Repeated: every session changed 2-3
   files, and a bare PersistentClient open followed only by count() changed 3. This is the risk
   recorded in Step 0, now confirmed. Fixed at the source, the same way as SQLite in Part 1:
   `search.open_collection(read_only_copy=True)` copies the index folder to a temporary folder and
   opens the copy; the copy is removed at exit. The server uses it. Retested: two full sessions, the
   sample folder and index byte-identical, no temporary copies left behind.
4. **Ctrl+C did not stop the server.** Two tests of mine were invalid first (feeding /dev/zero; then a
   bash background job, where bash makes the process ignore SIGINT). Tested properly from Python: no exit
   within 10 s of SIGINT; it stopped only when stdin closed. Cause read in the SDK: the stdio transport
   reads stdin through anyio.wrap_file on a worker thread, which the shutdown waits for. Fix: the server
   handles SIGINT and SIGTERM itself: logs, removes the temporary index copy, os._exit(0) (read-only, so
   nothing to save). Retested: exits 0.02 s after Ctrl+C with code 0; temporary copy present while
   running, gone after Ctrl+C; closing stdin (how clients stop a server) exits in 0.42 s, code 0.

**Checks in the build workspace:**
- In-process: 3 tools (all read_only_hint true), template jd://{role}, prompt shortlist_for_role;
  list_roles mode "sample" with both roles and titles; search_candidates returns ranked hits (pool 20
  for semi-senior); a filter (aca_acca_in_progress) narrows the pool to 3; get_profile, resource and
  prompt return the expected text.
- Real stdio subprocess (python src/mcp_server.py): handshake negotiated protocol 2026-07-28 with the
  SDK client and 2025-06-18 when a client asked for that; tools listed; list_roles, get_profile and
  jd://semi-senior worked over the pipe; stdout carried only protocol messages, logs on stderr.
- get_profile for an unknown id and for a client-staff id return the same "No candidate with id"
  error, so the tool never reveals who is client staff.
- 13 existing tests pass.

**Not exercised:** search_candidates over stdio with the real embedding model (blocked in the build
workspace; works in-process with a stand-in); the server on Windows; anything with the real data.
**For Step 4:** an unanticipated failure (such as the embedding model failing to load) currently
reaches the assistant only as the generic "Error executing tool search_candidates".

**Step 3 on Fabian's machine (14/10 16:16):** 13 tests passed; `python src\mcp_server.py` started
("starting: mode=sample data=...\sample ... (read-only, stdio)") and waited; Ctrl+C printed
"stopping (signal 2)" and returned the prompt. **Step 3 status:** everything identified is done.
**Not exercised:** a search through the server with the real embedding model.

## Step 4: Add the guards (14/10)

Most guards were built into Step 3. Added:
- **Unanticipated failures say what failed.** Search is wrapped: anything other than a ToolError is
  logged with its traceback to stderr and returned as "Search failed (<type>: <message>). The embedding
  model (about 80 MB) downloads on the first search on a machine; check the server log." Seen working
  in the build workspace, where the model download is blocked.
- **query** at most 500 characters (schema-level, max_length).
- **A filter combination with no candidates** returns an error saying to loosen it, not an empty list.
- **Prompt arguments:** unknown role and how_many outside 1-20 rejected. First version raised
  ValueError, which reached the client only as "Internal server error" (the SDK withholds plain
  exception text from prompts). Fixed: MCPError with JSON-RPC code -32602 (invalid params), which
  carries the message.
- **CANDIDATE_RAG_CHROMA_DIR** (paths.py): points the index elsewhere, used only so the check can run
  the server against a missing or empty index without touching the real one.

Read from the SDK: a server launched over stdio gets only a safe list of environment variables
(on Windows APPDATA, HOMEDRIVE, HOMEPATH, LOCALAPPDATA, PATH, PATHEXT, PROCESSOR_ARCHITECTURE,
SYSTEMDRIVE, SYSTEMROOT, TEMP, USERNAME, USERPROFILE) merged with any the client adds. So the model
cache under the user folder is found, and for Step 6 CANDIDATE_RAG_REAL would have to be set in the
client's config, not in a shell.

**Built: `tools\check_guards.py`.** Launches the real server over stdio and checks, PASS or FAIL: a
normal search returns 5 ranked results; results are snippets (300 characters at most); 21 and 0
results rejected; unknown role; unknown filter key; non-boolean filter value; empty query; over-long
query; made-up id; client-staff id (same error as made-up); a real id returns the full profile; unknown
role advert; prompt how_many over 20; prompt unknown role; missing index and empty index (servers
started on temporary folders) say "Run: python src/index.py"; sample folder and index fingerprinted
before and after every call above, unchanged.

**Build workspace result: 16 of 18 passed.** The 2 failures are the normal search and its snippet
check, which need the embedding model the workspace cannot download; the failure itself came back as
the new clear message. 13 existing tests pass.

**Not exercised:** the normal search over stdio with the real model (Fabian's machine).

**check_guards.py on Fabian's machine (14/10 16:22):** the first 17 checks PASSED, including the
normal search with the real embedding model (pool 20; top: 10 Semi Senior Accountant, 21 Accounts Semi
Senior, 11 Accounts and Audit Semi Senior) and snippets capped at 300 characters. Then the script
crashed in its own cleanup: PermissionError [WinError 32], chroma.sqlite3 in the temporary EMPTY test
index "being used by another process". Windows will not delete an open file; Linux will, which is why
it passed in the build workspace. The holder was the script's own chromadb client (it made that index
in-process). The read-only fingerprint check never ran.

**The same question for the server, not yet measured:** the server also deletes a temporary index
copy on exit, with ignore_cleanup_errors=True, so on Windows it could be failing silently and leaving
copies in %TEMP%. Changes:
- search.close_temp_copies(): stops Chroma's cached systems and clears its cache, then deletes the
  copies, and returns any that could not be removed. The server calls it on Ctrl+C/SIGTERM and when the
  client closes the connection, and logs a warning naming any copy it could not remove.
- check_guards.py: the empty test index is made in a separate process, so the script holds nothing
  open; temporary folders use ignore_cleanup_errors; a 19th check counts the server's temporary copies
  in the temp folder before and after the run (must be no new ones).
Build workspace: 17 of 19 (the 2 model searches), new check PASS; 13 tests pass. On Windows the
19th check is the real test.

**check_guards.py on Fabian's machine, second run (14/10 16:27):** before the run, **2**
candidate_rag_index_* folders were already in %TEMP%: left by the previous version's servers in the
16:22 run, so the old cleanup DID fail silently on Windows, as suspected. With close_temp_copies:
**19 of 19 passed**, including "server deletes its temporary index copies on exit: none left" and
"read-only: 9 files fingerprinted; changed: none". The 2 leftovers (copies of the invented sample
index) to be deleted by hand.

**Step 4 status:** everything identified is done.

## Step 5: Test it (14/10)

**Inspector:** `mcp dev` in SDK 2.3.0 runs the server through `uv run`, which this project does not
use, so the Inspector is launched directly with npx. Version pinned to **2.9.0** (30/09): the newest,
2.11.0, was published on 14/10, the same day, too new to trust; the newest release at least two weeks
old is 2.9.0. Needs Node.js >= 22.19. Learned by running it (its README is thin): the command-line
form is `--cli <command> <args> --method ...` (server command FIRST; a `--catalog` option or a `--`
separator made it report "No servers found in config file"); `--strict` reports tool-schema
portability problems; it keeps a catalog in ~/.mcp-inspector; secrets set to memory.

**The Inspector found a real bug:** `get_profile candidate_id=10` was rejected, "Input should be a
valid string ... input_value=10, input_type=int". The Inspector sends numeric-looking values as
numbers, and assistants are likely to do the same with ids like "10". Fixed: candidate_id accepts str or
int and is normalised to text. check_guards.py gained a case for it (20 checks).

**Built: `tools\inspector_check.py`** (12 checks via the Inspector CLI, PASS/FAIL): Node version;
tools/list lists exactly the 3 agreed tools, all readOnlyHint; --strict exit code 0; resource template;
prompt and its arguments; list_roles in sample mode; a 5-result search; 21 results rejected;
get_profile with a numeric id; made-up id rejected; advert read; prompt renders. Build workspace:
11 of 12 (the model search).

**Built: automated tests through the MCP layer.** `tests\conftest.py` pins every test to the invented
sample (removes CANDIDATE_RAG_REAL, sets CANDIDATE_RAG_SAMPLE and CANDIDATE_RAG_READONLY) and points
the index at a throwaway folder, so no test can read the real data. `tests\test_mcp_server.py` (19 tests
counting the parametrised guards) calls everything through the SDK's Client connected to the server
in-process; the throwaway index uses a deterministic stand-in embedder, so they test the MCP layer and
guards, not search quality. Covers: exactly the agreed tools/resource/prompt, all read-only, with
descriptions; sample by default; ranked snippets inside the hard filters with the right pool size; a
qualification filter narrowing the pool; client staff never returned by search or get_profile; text and
numeric ids identical; the advert resource; the prompt's rules; 8 bad tool inputs; bad resource and
prompt arguments; missing index; sample folder and index unchanged by a session.

**Build workspace: 32 passed** (13 original + 19). sample\ and data\ unchanged by the run; no temporary
folders left. **The tests bite:** removing the client-staff check from get_profile made exactly
test_client_staff_are_never_returned fail (1 failed, 31 passed); restored, 32 passed.

**Not exercised:** all three on Fabian's machine; the Inspector's browser interface (the CLI covers the
same calls).

**Step 5 on Fabian's machine (14/10 16:38-16:41):** pytest **32 passed**; check_guards.py **20 of 20**
(numeric id accepted; no temporary copies left); Node.js was not installed (the Inspector check stopped
with "Node.js not found"); installed Node v24.20.0; inspector_check.py **12 of 12** (5-result search
with the real model: 10 Semi Senior Accountant, 21 Accounts Semi Senior, 11 Accounts and Audit Semi
Senior, 16 Semi Senior Accountant, 13 Accounts Senior; --strict exit code 0).

**Step 5 status:** everything identified is done. **Not exercised:** the Inspector's browser interface.

## Step 6: Connect it to Claude (14/10)

From the official guide (modelcontextprotocol.io, "Connect to local MCP servers", fetched 14/10): open
the config through Claude desktop Settings > Developer > Edit Config (Windows file:
%APPDATA%\Claude\claude_desktop_config.json); `mcpServers` entries take `command`, `args`, optional
`env`; paths must be absolute; quit and restart the app fully; tools appear under the "+" (Add files,
connectors and more) > Connectors > Manage connectors; logs in %APPDATA%\Claude\logs (mcp.log, and
mcp-server-<name>.log holding the server's stderr).

Config used: command = the project's venv python (absolute), args = src\mcp_server.py (absolute), no
env, so the server runs on the sample (Step 2 decision).

**Desktop config:** Fabian's claude_desktop_config.json already held the app's own preferences, so
it was merged, not replaced: the existing JSON parsed, `mcpServers.candidate-rag` added, and checked
that every original setting was identical. Pasted into the file the app's Edit Config opened (the
install looks like the Microsoft Store package, whose settings path can differ from %APPDATA%).
The three tools showed as needing approval; set to Always allow (all three read-only, on the sample).

**First run in the Claude desktop app (14/10 16:55), sample, prompt "Use the candidate-rag tools to
shortlist the 3 best candidates for the semi-senior role, with an evidence quote from each profile":**
- Picks: 10 (Semi Senior Accountant), 21 (Accounts Semi Senior), 11 (Accounts and Audit Semi Senior).
  **All three are strong in the invented answer key; no false positives.** Left out 13 (key strong) as
  an Accounts Senior since 2022 for whom the role would be a step down: a defensible call.
- **All 5 quotes verified word for word** against sample/profiles.jsonl, and the career facts it gave
  (dates, promotions, previous roles) are right. It flagged its own limits correctly: "likely held" AAT
  for 21, location only "United Kingdom" for 21, and that no profile mentions VAT or personal tax work.
- Tool calls not yet checked (which tools, in what order): to be read from the chat.

**Bug found by the run, in the invented sample (not the server): client staff flagged on the wrong
people.** The sample adverts name the fictional hiring firm as Ashby Pell, but make_sample.py
flagged id 8 (at a different firm) as client_staff and left ids 1 and 18, both at Ashby Pell,
unflagged, so Claude listed candidate 18 as a close call. The server's rule worked as built; the
data was wrong. Fixed in tools\make_sample.py: id 8 now works at Ashby Pell (the only client staff);
ids 1 and 18 moved to invented firms; labels unchanged. Regenerated; 32 tests pass; guards 18 of 20 in
the build workspace (the 2 model searches).

**Second desktop run (14/10 17:01), after the sample fix and a full restart of the app:** picks 10, 11,
21, all strong in the answer key, no false positives; 13 left out again as a step down; 18 now shown at
Corwen Tilt and the Ashby Pell employee never appears; Claude noticed the "sample" mode and pool of 20
and said so. All 3 quotes verified verbatim. Tool order (Fabian, from the chat): list_roles, then
search_candidates, then get_profile.

**Server log** (found at %LOCALAPPDATA%\Claude\logs\mcp-server-candidate-rag.log, NOT the %APPDATA%
path the official guide gives): both runs made three searches for semi-senior, one broad (n=15, pool
20) and two with qualification filters Claude chose itself (aat_qualified: pool 3; aca_acca_in_progress:
pool 3), so it searched from more than one angle. Pool sizes match the sample. The app starts the server
twice on each launch, a second apart (harmless). On quitting the app at 17:00:16 the server logged
"stopped (client closed the connection)"; the one temporary index copy in %TEMP% afterwards was created
17:01:22, one second before the live server's first search, so it belongs to the running server and the
closed session cleaned up.

**Step 6 status:** everything identified is done on the sample. **Step 6.4 (real data through the
desktop app) not done**, on the recommendation recorded in Step 0: it would send real candidates'
profiles to Anthropic as conversation content under a personal account, and the CV claim needs only the
sample. Fabian, 17:15: "let's get this finished". No screenshot taken.

## Step 7: Publish (14/10)

README: new section "MCP server: let any assistant search the pool" (what it exposes; sample by
default; read-only by index copy; guards; what building it caught; the three check suites; the desktop
run; how to connect, with placeholder paths); test count 13 -> 32; layout lines updated. Then the
leak check, git status, commit, push.

**First leak check (17:20): 2 hits, both false.** Pytest 32 passed. check_public.py flagged "email
address" at tools/inspector_check.py lines 4 and 23: both are the Inspector's version pin,
`@modelcontextprotocol/inspector@2.9.0`, which the email pattern matched (a name, an at sign, then numbers and dots).
Not real data. Fixed in the checker rather than by allow-listing the file: the domain must now start
with a letter and end in a letters-only suffix. Checked against four real-shaped addresses (all still
caught, including a hyphenated .co.uk) and four version pins (none caught). git status listed nothing
from data/, jds/, chroma/, sample/out/ or sample/chroma/.

**Second leak check: 1 hit, my own wording.** The log entry above described the false match as
a made-up example address (a word, an at sign, then words and dots), which is itself shaped like an email address. Reworded; the rescan was CLEAN.

**Published (14/10, 17:24): commit 4e20187 on main**, 15 files changed. Checked from outside on a fresh
clone of github.com/fabianaspey-droid/candidate-rag: 31 files, none from data/, jds/, chroma/,
sample/out/ or sample/chroma/, no .env, no client or agency name, no LinkedIn URL, no email address;
installed from requirements.txt on Linux (Python 3.13) and ran the tests: 32 passed, and the run left
no new files in the clone.

**Part 2 status:** everything identified is done. Not exercised: the real pool through the desktop app
(Step 6.4, skipped on purpose), the Inspector's browser UI (only its CLI was used), check_guards.py and
inspector_check.py on the fresh clone (they need the real embedding model download and Node), and the
server on macOS or Linux through a desktop client.

# Part 3: Remote MCP server on AWS (from 20/10/2026)

Build guide: the "Candidate RAG remote MCP on AWS: build guide" document (20/10/2026). Fabian's brief: a
remote MCP server on the 22-person sample that an interviewer can add to Claude by URL and try; compare ECS
Express Mode with Lightsail; avoid IAM Identity Center and AWS Organizations; budget alert before any
resource; London (eu-west-2); stages: HTTP + Inspector locally, container locally, ECR and deployment,
CloudWatch logs, GitHub Actions. Same eight working rules as Part 2.

**Decisions taken in the guide:** ECS Express Mode (Lightsail can't use private ECR or CloudWatch, so it
fails stages 3 and 4); authless on the sample only; Streamable HTTP, stateless, JSON responses; 0.25 vCPU
and 1 GB (measured about 306 MB with a same-size stand-in model; the real model's download is blocked from
the build workspace); about $38 a month at US East rates, London to be confirmed in the Pricing Calculator.
AWS sign-up deferred to just before Step 4, because the six-month free plan starts at sign-up.

**Corrections to the brief, named at the time:** joining AWS Organizations upgrades a free plan account to
paid, but the docs don't say the credits are removed; Identity Center isn't named as a trigger. App Runner
closed to new customers is confirmed, but not the 30 April date. The free plan also ends when the credits
run out, not only after six months.

## Step 1: Windows set-up, WSL 2 and Docker Desktop (20/10)

**Machine check:** Windows 11 Home; 17 GB RAM; virtualisation enabled in firmware; WSL not installed.

**Windows Home and Docker Desktop:** Docker's install page contradicts itself. It lists Enterprise, Pro
and Education as supported, but also says "Windows Home or Education editions only allow you to run Linux
containers", which implies Home works for Linux containers, the only kind this build uses. Decision:
install and let the Step 1 checks decide; fallback if Docker Desktop won't run is Docker Engine inside a
WSL Ubuntu distribution, which doesn't depend on the Windows edition.

**Build 26200 (Windows 11 25H2), WSL 3.0.3.0 installed, but WSL 2 couldn't start:** "virtualization is
not enabled". Firmware virtualisation was already True, so the BIOS was ruled out; HypervisorPresent was
False. Checked rather than guessed: in an elevated window VirtualMachinePlatform showed Disabled. The first
install had run in a non-admin window (the feature query also failed there with "requires elevation"), so
only the WSL package got installed, not the Windows component. `wsl --install --no-distribution` as
administrator enabled it ("Changes will not be effective until the system is rebooted"). My mistake on the
way: the first feature query passed two names to -FeatureName, which takes one.
After the restart: `wsl --status` shows Default Version 2 with no virtualisation error; HypervisorPresent
True. WSL 2 is working. (The WSL1 message remains and doesn't matter; only WSL 2 is used.)

**Docker Desktop installed on Windows 11 Home (20/10):** Docker Desktop 4.96.0, engine 29.9.1,
context desktop-linux; Server section present with OS/Arch linux/amd64. `hello-world` printed "Hello from
Docker!". `python:3.13-slim` printed `x86_64 3.13.16` (image digest sha256:70729b46c69b4f1e97c4822c1af3df5
3a1476cf5ddc6c087c0c10bc3a5678c2f, the candidate to pin in the Dockerfile). The Windows Home question is
settled for this build: Linux containers run, so the Docker Engine-in-WSL fallback isn't needed.

**Step 1 status:** everything identified is done. Not exercised: building our own image (Step 3).

## Step 2: HTTP transport, locally and in MCP Inspector (20/10)

**How files reach the project from now on:** Fabian chose option A at 17:29. The chat is linked to his PC
and the candidate-rag folder is connected, so files are written straight into it; no more downloading and
moving. The folder also holds data/, chroma/ and .env; the rule is that they are never read or staged, and
only named files are written or read back.

**What changed:**
- `src/mcp_server.py`: `--http` (or MCP_TRANSPORT=http) serves Streamable HTTP at /mcp; stdio stays the
  default, so the desktop config is untouched. Stateless, JSON replies. Host check passed explicitly
  (localhost always, plus MCP_ALLOWED_HOSTS); origins claude.ai, claude.com and local ones (the
  Inspector's web UI). `GET /health` outside the Host check. Warm-up (profiles, index copy, one embedded
  query) runs *before* the server starts listening, and a failed warm-up exits with code 1.
  Signal handler now also covers SIGBREAK (Windows Ctrl+Break).
- `tests/test_http.py`: 8 tests through Starlette's test client (health with an IP Host header, 421 for a
  wrong host, 403 for a foreign origin, a stateless call with no session id, the same tools/resource/prompt
  as stdio, guards over HTTP, the host-pattern helper). No search, so no index or model needed.
- `tools/check_http.py`: 11 checks against a real server process it starts and stops itself, or against a
  running one with `--url` (Steps 3 and 4).
- `tools/inspector_check.py`: `--http URL` runs the same 12 checks over Streamable HTTP.

**Change from the guide, decided while building:** the guide said /health returns 503 during start-up.
Instead the server doesn't listen until warm-up has finished. Reason: a 503 phase needs the warm-up on a
second thread while requests are already arriving, so two requests could each open their own index copy
(the collection cache isn't safe across threads). Not listening is simpler and the load balancer treats it
the same way (unhealthy until it answers). The check is now "nothing listening, then 200, with 'warm:'
logged before 'Uvicorn running'".

**Found while testing:** the SDK's HTTP transport logs "Terminating session: None" at INFO for every
stateless request, which would be one useless CloudWatch line per call; its logger is set to WARNING.
The log format also hard-coded "candidate-rag:" for every logger, so SDK lines looked like ours; it now
prints each logger's own name (ours is still candidate-rag).

**Checked in the build workspace before handing over** (Linux, Python 3.13, with a stand-in for the
embedding model laid out the way Chroma expects, because the real download is blocked there):
pytest 40 passed (32 + 8); check_http 11/11 (ready after 2.2 s, nothing listening before warm-up, 421, 403,
stateless, search, guards, 10 parallel searches, SIGTERM exit 0 in 0.3 s, no copy left); inspector_check
--http 12/12 including --strict; inspector_check (stdio) 12/12; check_guards 20/20. Mutation check:
switching the host protection off makes the 421 and 403 tests fail.

**Not yet exercised:** any of this on Windows, with the real model; Ctrl+Break as the stop signal (Windows
has no SIGTERM for a child process).

**Fabian's run on Windows with the real model (17:43 to 17:45):** pytest 40 passed. check_http 11/11:
ready after 3.7 s with one earlier attempt finding nothing listening and 'warm:' logged before 'Uvicorn
running'; 421 for another host; 403 for a foreign origin and 200 for claude.ai; stateless call, no session
header; search, get_profile and guards over HTTP; 10 parallel searches in 3.0 s; **Ctrl+Break stop: exit
code 0 after 0.2 s, 'stopped' logged, no temporary copy left**. check_guards 20/20 and inspector_check
(stdio) 12/12, so stdio is unchanged. Manual run: warm-up 0.6 s; inspector_check --http 12/12 including
--strict; Ctrl+C gave "stopping (signal 2)" then "stopped". The real model ranks the same five semi-seniors
in the same order over stdio and over HTTP (10, 21, 11, 16, 13).

Seen in the server output: the Inspector opens a GET /mcp stream after each initialise (200), harmless in
stateless mode; uvicorn logs one access line per request and the SDK logs rejected tool arguments at INFO,
both useful in CloudWatch.

**Step 2 status:** everything identified is done. Not exercised: a load balancer, HTTPS, and SIGTERM on
Linux (Windows used Ctrl+Break and Ctrl+C); those come in Steps 3 and 4.

## Step 3: The container, running locally (20/10)

**New files:**
- `requirements-server.txt`: the full dependency set for the image, 93 packages, generated by installing
  chromadb==1.5.9 and mcp==2.3.0 into a clean Linux Python 3.13 venv and running `pip freeze`, so every
  build installs the same versions. No anthropic, no pytest. python-dotenv appears because chromadb
  depends on it; the server never reads .env and the build never sees it.
- `Dockerfile`: python:3.13-slim pinned by the digest pulled in Step 1; dependencies in their own layer;
  the model downloaded and the sample index built at build time, as root, then made read-only for the run
  user; HOME fixed at /opt/candidate-rag-home so the build-time model is the run-time model; user 10001,
  no home, no login shell; ANONYMIZED_TELEMETRY=False; MCP_TRANSPORT=http, MCP_HOST=0.0.0.0, port 8000;
  CMD in exec form so SIGTERM reaches Python directly.
- `.dockerignore`: an allowlist. `*`, then only requirements-server.txt, five server source files
  (mcp_server, search, paths, index, retrieve), sample/profiles.jsonl and sample/jds/. The Part 1 scripts
  that call Claude or read the source database aren't in the image at all.
- `tools/check_container.py`: 11 checks on the image and on a container run read-only with the Fargate
  limits; reuses check_http.py (--url) and inspector_check.py (--http). GitHub Actions will run it in Step 6.

**Checked in the build workspace before handing over.** Docker Hub is blocked from there, so the build used
a stand-in Python 3.13 base assembled from the workspace's own Python (only the FROM line changed), and the
stand-in embedding model. Planted a fake data/profiles.jsonl, a fake .env and a jds/ file in the build
copy to test the allowlist. Results: image 810 MB (with the stand-in base); 11/11 checks: user 10001; no
real-data switch; exactly the 9 allowlisted files plus 5 sample-index files; none of data/, jds/,
chroma/, .env, .git, .venv; no anthropic; healthy after 6.7 s with --read-only --tmpfs /tmp, 1 GB, 0.25
vCPU; check_http 10/10 against it; Inspector 12/12 against it; docker diff empty; docker stop exit 0 in
0.7 s with 'stopped' logged. Memory 101 MiB, which means nothing with a stand-in model.

Found while building there, both from the stand-in base, not the Dockerfile: missing system libraries
(libpthread), and useradd not on PATH. The real python:3.13-slim has both.

**Not yet exercised:** the real base image and the real model, so the real image size and memory.

**Fabian's first run on Windows (18:07):** build 80.8 s; image 1.16 GB on disk, 341 MB content (compressed).
check_container at 1 GB: 10/11. Image checks all passed; healthy after 23.0 s at 0.25 vCPU; Inspector 12/12;
docker stop exit 0 in 0.9 s, not OOM-killed. **check_http failed**: its output stopped after the guards
check, with no summary, so it crashed during "10 searches at once", and check_container hid the error. At
512 MB: also check_http crashed, and Inspector 7/12.

**Queried before proposing a cause (rule 2).** Reproduced in the build workspace with a stand-in model
that has the real model's size *and* compute (6 layers, 30,522 x 384 embeddings), container at 1 GB and
0.25 vCPU: the same crash, as "Server disconnected without sending a response", and here the container
was **OOM-killed (exit 137) at 1 GB**. Measured without a limit: idle 109 MiB, ten searches at once about
1.1 GiB, falling back afterwards. Isolated it in-process:
- ONNX Runtime alone, ten concurrent runs: +30 MB. Not the cause.
- Chroma embedding x10 concurrent: +217 MB; query x10: +345 MB; full hybrid search x10: +330 MB.
- The same searches one after another, same thread or new threads: flat (179 to 215 MB after 30).
So the cost is concurrency: each search in progress holds its own working memory, and the SDK runs every
tool call on its own worker thread (anyio.to_thread, checked in the SDK source).

**Fix, at the source (rule 4):**
1. `src/mcp_server.py`: a lock so one search runs at a time (also makes the first index open thread-safe).
   On 0.25 vCPU parallel searches can't finish sooner anyway, so queuing costs no throughput. With the
   lock alone, 20 queued searches still peaked at 755 MiB: glibc gives each worker thread its own memory
   arena and keeps what it frees.
2. `Dockerfile`: MALLOC_ARENA_MAX=2. With both: 20 queued searches peaked at 286 MiB, all answered, slowest
   21.2 s (each search about 1 s at a quarter of a CPU). With arenas capped and no lock, the in-process
   burst still reached 669 MB, so both are needed.
- `tests/test_mcp_server.py`: new test that six concurrent searches never overlap. Mutation check:
  without the lock it fails ("assert 4 == 1"). pytest now 41.
- `tools/check_http.py`: the parallel-search check reports a dropped connection as a failure instead of
  crashing the script.
- `tools/check_container.py`: prints the failing lines and the error output of a sub-check, and samples
  memory every half second for the whole run, so it reports the *peak* and any OOM kill, not the level
  afterwards (the first version would have missed this).

**Rechecked in the workspace with the realistic stand-in model:** check_container 11/11 at 1 GB (peak 256
MiB) and 11/11 at 512 MB (peak 283 MiB), never OOM-killed; check_http 10/10 and Inspector 12/12 against
the container at both sizes.

**Size decision:** stays at 1 GB. 512 MB now passes too, but with about 230 MB of headroom against a
measured peak, and an out-of-memory kill gives no useful error; 1 GB costs about $1.60 a month more.

**Fabian's rerun on Windows (21/10, 10:26):** pytest 41 passed. Build 79.8 s. check_container at 1 GB:
10/11, **peak memory 273 MiB over 64 samples, never OOM-killed** (the fix worked); at 512 MB: 10/11, peak 243
MiB. Both: healthy after about 18 s, Inspector 12/12, docker stop exit 0. **Still failing: "10 searches at
once"**, at both sizes, and check_container printed only the check's name, not its detail line, so the
cause isn't visible. Not guessing: check_http now times the single search and every one of the ten (each
one's wait, and any error such as a timeout), and check_container prints the failing check's detail line.
The 60 s per-request timeout in that check is the obvious suspect if each search on this laptop's quarter
CPU takes several seconds (healthy took 18 s here against 6.5 s in the build workspace), but that's to be
measured, not assumed.

**The detail (21/10, 10:33):** "6/10 in 60.9 s; each waited 12.7 to 60.9 s; errors: ReadTimeout". Memory
peak 366 MiB, never OOM-killed. So nothing crashed: searches were queued as designed, but each took roughly
10 s on this laptop's quarter CPU, and the last four passed the check's 60 s per-request limit. That's not
only a test problem: a demo where Claude runs four searches would take 40 s.

**Measured where the time goes, inside the container at 0.25 vCPU (build workspace):** embed the query 1.0 s;
query the index by vector 0.02 s; BM25 0.0003 s; whole search 1.0 s. The query embedding is the entire cost.
Two causes, each measured: Chroma pads every input to 256 tokens (a 12-token query embeds in 0.04 s instead of
0.28 s), and ONNX Runtime starts one thread per host core, which then fight over a quarter of a CPU (one
thread: 0.18 s against 0.28 s even on this 2-core workspace; worse on a laptop with many cores).

**Fix at the source:** `src/search.py` gains QueryEmbedder: the same model file, tokenizer, truncation at 256,
mean pooling and normalisation as Chroma's default embedding function (copied step for step from chromadb
1.5.9), but no padding and one ONNX thread. `hybrid_rank` takes it as an optional `embed`; without it nothing
changes, so retrieve.py and the Part 1 agent behave exactly as before. The server uses it for searches and the
warm-up, so Chroma's own model session is never loaded at run time.

**Equivalence is measured, not assumed:** new `tools/check_query_embedding.py` compares QueryEmbedder with
Chroma's function on 34 inputs (10 queries, all 22 profiles, both adverts, which run past 256 tokens) and
requires cosine similarity of at least 0.99999; then checks that the semantic ranking of each whole pool and
the hybrid ranking search_candidates returns are identical for every query and role (20 each); and times both.
In the workspace it passes 3/3, but there the stand-in model ignores attention, so that only proves the
script works; the real proof is Fabian's run with the real model.

**Results in the workspace (realistic-cost stand-in, 0.25 vCPU):** full search 0.038 s each, against 1.0 s
before; check_container 11/11, peak memory 206 MiB. pytest 41 passed (the server tests give the server the
same stand-in for queries as for the index, so they still need no model download). The one-search-at-a-time
lock and MALLOC_ARENA_MAX=2 stay: cheap, and they still bound memory if traffic arrives together.

**Fabian's run with the real model (21/10, 11:02):** pytest 41 passed. **check_query_embedding 3/3: lowest
cosine similarity 1.00000000, largest difference in any dimension 0.00e+00** (bit-for-bit the same vectors as
Chroma's, on all 34 inputs including both adverts past 256 tokens); semantic and hybrid rankings identical
20 of 20; query embedding on the laptop, unrestricted, 24 ms with Chroma's function against 4.3 ms. Rebuild
23.6 s (dependency layer cached). check_container 11/11 at 1 GB / 0.25 vCPU: healthy after 13.1 s; check_http
10/10 including the ten searches at once; Inspector 12/12; **peak memory 209 MiB**; docker diff empty; docker
stop exit 0 in 0.8 s.

**Step 3 status:** everything identified is done. Image: 1.16 GB on disk, 341 MB compressed content. Size
stays 0.25 vCPU / 1 GB. Not exercised: Fargate's own hardware and network, pulling the image from a registry,
and an explicit check that search lines reach `docker logs` (the start-up and "stopped" lines do, which is
the same stream; Step 5 checks search lines in CloudWatch). The 512 MB comparison was last run before the
speed fix (peak 243 MiB then).

**Checkpoint (21/10, 11:16), local only, not pushed:** check_public CLEAN (38 files, 6 private terms, 465
employers, 232 names). `git status` listed 12 files, none under data/, jds/ or chroma/. Commit a3fff01, "Part
3 Steps 1-3: HTTP transport, container, memory and speed fixes", 12 files, 1,310 insertions.

## Step 0: AWS account (21/10)

**The account was created through AWS's new sign-up experience, and its Region is fixed.** Fabian (11:57):
"My current region is automatically assigned (eu-north-1 (Europe - Stockholm))", with London apparently not
available. Checked the docs ("AWS Regions for your projects", "Compare sign-up options"): the new
experience assigns the project to one of three Regions by the country in the contact address (us-east-2,
eu-north-1, ap-southeast-2; the United Kingdom maps to Stockholm), and "You cannot modify your AWS Region".
The only ways to another Region are activating advanced features ("Activation cannot be reversed"; it also
turns on AWS Organizations and IAM Identity Center, the things the brief avoids) or a separate account through
"Sign up for AWS (advanced)".

**Claims in the guide this breaks, named so they get corrected:** (1) "Region: eu-west-2 (London)". (2) Step
0's root-user MFA and console IAM user: in the new experience there is no root user (an AWS Builder ID owns
the account), and IAM users "can only be used for programmatic access, not console access". (3) The guide
didn't know spend limits exist here; they need the paid plan, so they don't apply on the free plan.

**Decision, 21/10:** stay in Stockholm (eu-north-1). Fabian carried on with Step 0 on that basis. Reason as
put to him: London was there to keep logs in the UK, which mattered for real data; the server only ever holds
the invented sample; Stockholm is in the EU and runs ECS, Fargate and load balancers the same way; moving
would need an irreversible change (advanced features) or a second account.

**MFA:** already registered on the Builder ID at sign-up, which in this account type is the AWS login itself.

**Budget** "AWS - RAG server": cost budget, $50 monthly, recurring, unblended; scope "Filter specific AWS
cost dimensions" with **Charge type Credit and Refund excluded**. That option is no longer under Advanced
options in the console, which now says to use Charge type as a filter dimension. AWS counts credits by
default (IncludeCredit defaults to true), so without this the budget would read about zero until the credit
ran out. Alerts at 50% and 80% actual and 100% forecasted, to Fabian's email.

**Credit activities listed (0 of 5, US$20 each):** launch an EC2 instance; use a model in the Bedrock
playground; set up a cost budget; create a web app with Lambda; create an Aurora or RDS database. The budget
activity still showed "Not started" just after the budget was created; to recheck later.

**CLI, with no access keys:** AWS CLI 2.38.3. `aws login` asked for a Region first (eu-north-1 given), then
offered to configure AWS skills and an AWS MCP server for AI coding agents (declined: "n"; it would change
other tools' config files and isn't needed). Sign-in through the Builder ID worked. `aws sts
get-caller-identity` returns an **assumed role, `AccountFullAccessRole`**, not an IAM user, which is how this
account type gives console identities programmatic access; region eu-north-1; no `.aws\credentials` file
(Test-Path False). The account ID is deliberately not written here, because this log is public.

**Price, AWS Pricing Calculator, Europe (Stockholm), 21/10:** Application Load Balancer (1 ALB; 0.01 GB/hour
processed to IP targets, 1 new connection/s, 1 s duration, 1 request/s, 1 rule evaluation), Fargate (Linux,
x86, 1 task a month for 730 hours, 0.25 vCPU, 1 GB, 20 GB ephemeral), 3 in-use public IPv4 addresses:
**US$40.35 a month**, no upfront cost. In line with the guide's estimate of about $38 at US East rates. The
billing page shows the Free plan with the US$100 credit: about 2.5 months of running time, more if the
other four credit activities are earned.

**Consequence to plan for:** on the free plan the account closes when the credits run out. So the service is
deleted when not needed for interviews, and well before the credit is used up, unless Fabian decides to
upgrade to the paid plan.

**Step 0 status:** everything identified is done. Not exercised: the budget alert emails (they fire only on
real spend) and whether this account type allows Fargate and a load balancer; both come in Step 4.

## Step 4: ECR and deployment to ECS Express Mode (21/10)

**Go-ahead:** Fabian, 12:33, "Yes", after the cost was stated: US$40.35 a month (about US$1.33 a day) from
the Pricing Calculator for Stockholm, covered by the US$100 credit for about 2.5 months; on the free plan the
account closes if the credit runs out, so the service gets deleted when not needed.

**Checked against AWS's documentation before writing:** the Express Mode getting-started page (role names
`ecsTaskExecutionRole` trusted by ecs-tasks.amazonaws.com and `ecsInfrastructureRoleForExpressServices`
trusted by ecs.amazonaws.com; new roles can take about a minute before Express Mode can use them); the managed
policy ARNs (`service-role/AmazonECSTaskExecutionRolePolicy`, `service-role/AmazonECSInfrastructureRolefor
ExpressGatewayServices`, the latter v6, scoped to resources tagged AmazonECSManaged); the API references for
CreateExpressGatewayService, UpdateExpressGatewayService and describe (the URL is at
`activeConfigurations[].ingressPaths[].endpoint`). Not verified: the exact name of the delete command, and
whether this account type allows Fargate and a load balancer at all.

**New files:**
- `deploy/trust-ecs-tasks.json`, `deploy/trust-ecs-infrastructure.json`: the two roles' trust policies.
- `deploy/ecr-lifecycle.json`: keep the newest three images.
- `deploy/express-service.json`: everything about the service that's safe to publish: name, 256 CPU units,
  1024 MiB, x86_64, health path /health, container port 8000, scaling min 1 / max 1, tags. No account number:
  the role ARNs and the image address are filled in at run time.
- `tools/aws_deploy.py`: one command per part of the step (check, roles, repo, push, create, status,
  delete), each safe to rerun. `push` refuses uncommitted changes and tags the image with the commit, so
  every running image traces to exact code. `create` fills a temporary settings file (deleted straight after,
  because it holds the account number), waits for the URL, then updates MCP_ALLOWED_HOSTS to the service's
  hostname, keeping every other container setting (the update sends the whole current container back, so
  the log configuration can't be reset). `delete` asks for the service name, then checks no load balancer is
  left. No task role: the server calls no AWS service, so it has no AWS permissions at all.

**Exercised in the build workspace against a fake AWS CLI and fake Docker** (no real AWS reachable there):
every command's path, reruns skipping what exists, push refusing a dirty tree, the update payload keeping
awsLogsConfiguration, delete refusing a wrong name. That proves the script's logic, not AWS's behaviour.

**Fabian's run (21/10, 14:29):** check_public CLEAN (43 files); commit 5634fe9, 6 files. `check`: assumed role
AccountFullAccessRole, eu-north-1, **default VPC with 3 public subnets in 3 zones**, nothing created yet,
local image present. `roles`: both created, each with its managed policy. `repo`: created, immutable tags,
scan on push, lifecycle policy set. `push`: the build was fully cached and tagged with the commit, then
**`docker login` failed: "error storing credentials - err: exit status 1, out: The stub received bad data."**
Nothing was uploaded.

**Cause, checked rather than guessed:** a known Windows limitation (aws/aws-cli issue 5636). Docker on Windows
stores registry logins in Windows Credential Manager, which holds secrets of about 2,500 characters; ECR's
login token is longer for temporary role sessions, which is exactly what `aws login` gives
(AccountFullAccessRole). AWS's own answer there: no fix in the CLI. Workarounds listed include removing
Docker's credential store, which would leave logins in plain text permanently.

**Fix:** `push` now logs in and pushes with a throwaway Docker settings folder (DOCKER_CONFIG) that has no
credential store, deleted as soon as the push ends, so the 12-hour token never lands anywhere permanent and
Fabian's normal Docker settings are untouched. DOCKER_HOST is set to Docker Desktop's current engine, which
would otherwise be lost along with the default settings folder. Tested against the fake Docker: both login
and push saw the temporary folder and the Desktop engine, and no folder was left behind.

**Fabian's rerun (14:32): the same error.** Commit b493b4a; push built from it, then `docker login` failed
again with "The stub received bad data." **My first fix was wrong.** Read the Docker CLI source
(docker/cli, cli/config/config.go and configfile/file.go): when loading the settings file,
`if !configFile.ContainsAuth() { configFile.CredentialsStore = credentials.DetectDefaultStore(...) }`, and
ContainsAuth is false when there is no credsStore, no credHelpers and **no entries in auths**; on Windows
DetectDefaultStore returns "wincred" whenever docker-credential-wincred is on PATH. The temporary file held
`{"auths": {}}`, an empty list, so Docker fell straight back to Windows Credential Manager.

**Fix:** the temporary file now holds one empty entry for the registry, `{"auths": {"<registry>": {}}}`
(the "empty auth config for your registry" workaround in aws/aws-cli issue 5636), so ContainsAuth is true,
no default store is detected, and the login is written to the temporary folder, deleted after the push.
Tested against a fake Docker that applies the same rule: the previous version fails with the same message,
the new one logs in and pushes.

**Push succeeded (21/10, 14:34):** commit d33cc2a; image `candidate-rag-mcp:d33cc2a2cf39` pushed to ECR in
Stockholm, 12 layers, digest sha256:bf1fb564... Docker printed its standard warning that the login was
stored unencrypted in the temporary folder's config.json, which is what the fix intends: that folder is
deleted when the push ends. Scan result reported as "none". Not yet confirmed: whether "none" means a
completed scan with no findings, or a scan that didn't run (Docker Desktop pushes an image index with an
attestation manifest, which basic scanning may not handle); checking the scan status before `create`.

**The scan never ran (14:35):** `describe-image-scan-findings` for tag d33cc2a2cf39 returned
"ScanNotFoundException ... Image scan does not exist". The temporary Docker folder was gone, as intended.
Cause, from the build output itself: "exporting attestation manifest" and "exporting manifest list". Docker
Desktop's builder wraps the image in an image index with a provenance attestation by default, the tag points
at the index, and ECR's basic scan-on-push doesn't scan an index. The helper had printed this as "findings:
none", which was misleading: a scan that didn't run looked like a clean one.

**Fix:** `push` builds with `--provenance=false --sbom=false`, so one plain image is pushed and scanned; and
it now reads the scan *status*, stops with FAIL unless it is COMPLETE, and only then reports the findings.
Tested against the fake AWS: a completed scan reports its findings; no scan stops with FAIL. The image
already in ECR (d33cc2a2cf39, an index) stays until the lifecycle rule removes it; nothing will run it.

**Plain image pushed (14:37):** commit 3c123b2; `push` built with --provenance=false --sbom=false (no
attestation or manifest-list lines this time), every layer already in ECR, digest sha256:68ad9eab...
**Scan COMPLETE: HIGH 3, MEDIUM 9, LOW 3, no CRITICAL.** The three HIGH findings, all in the base image's
Debian packages, none in our code: CVE-2026-19499 (glibc 2.41-12+deb13u4), CVE-2026-85091 (zlib
1.3.dfsg+really1.3.1-1), CVE-2026-5435 (glibc 2.41-12+deb13u4). Not assessed for exploitability here (no CVE
details looked up yet). Decision: proceed, under the agreed rule (no CRITICAL); the remedy when Debian ships
fixed packages is a newer pinned python:3.13-slim digest and a rebuild, a deliberate change rather than an
unpinned `apt-get upgrade` that would make builds unrepeatable.

**Plain image pushed and scanned (14:37):** commit 3c123b2; image `candidate-rag-mcp:3c123b2ce367`, all layers
already in ECR, one plain manifest (no "attestation manifest" in the build output this time). **Scan
COMPLETE: 0 critical, 3 high, 9 medium, 3 low.** The three HIGH findings are all in the Debian base image's
operating-system packages, not in our code or Python packages: CVE-2026-19499 and CVE-2026-5435 in glibc
2.41-12+deb13u4, and CVE-2026-85091 in zlib 1.3.dfsg+really1.3.1-1. Not assessed in detail. Decision:
proceed (the agreed bar was no CRITICAL), and record the follow-up: move to a newer pinned python:3.13-slim
digest once Debian ships fixed glibc and zlib, rebuild, rescan.

**`create` failed (14:41), nothing created, no charge:** "InvalidParameterException ... Unable to assume the
service linked role. Please verify that the ECS service linked role exists." This is ECS's own account-wide
role, AWSServiceRoleForECS, not one of the two roles made in `roles` (those were 12 minutes old, so not a
propagation delay). AWS's Express Mode getting-started page mentions this error. Normally ECS creates the role
on first use; in this brand-new account it evidently hadn't. Fix: `roles` now creates it with `aws iam
create-service-linked-role --aws-service-name ecs.amazonaws.com` if it's missing (free; the role is managed by
AWS), and `check` reports it. Tested against the fake AWS: created when missing, skipped when present.

**The real cause, from CloudTrail (21/10, 14:51 to 14:54).** `status` showed the service ACTIVE but 0 of 1
tasks running. Service events: "deployment failed: Service deployment rolled back because PROVISIONING
lifecycle hook(s) failed. AccessDenied: The infrastructure role ... doesn't have enough permission." No task had
ever started. CloudTrail (eu-north-1, and us-east-1 for IAM):
- 13:42:47 UTC: the infrastructure role creates AWSServiceRoleForElasticLoadBalancing, and the same second
  CreateLoadBalancer is refused ("AccessDenied: An unknown error occurred").
- 13:42:48: it creates AWSServiceRoleForApplicationAutoScaling_ECSService, and the same second
  RegisterScalableTarget fails: "Unable to assume IAM role ... AWSServiceRoleForApplicationAutoScaling_ECSService".
- 13:42:54: retries; both roles "has been taken in this account" (they now exist), still not usable; refused again.
- 13:42:56: deployment rolled back.
A first-use race in a brand-new account: AWS's own service-linked roles created on demand and used before IAM
had propagated them. Also refused, and judged not to be the cause because AWS's own managed policy doesn't
grant them, so Express Mode must tolerate it: ecs:DescribeServices, ecs:UpdateService, cloudwatch:DescribeAlarms,
ec2:DescribeAccountAttributes. To revisit if the retry still fails.

**Correction to the earlier entry:** AWSServiceRoleForECS was created at 13:40:21 UTC, by the CLI's own role,
at the moment of the first `create`. So ECS did create it automatically; that first failure was the same
propagation race, the "wait and retry" case AWS's getting-started page mentions, not a missing role.

**Fix:** `roles` now creates all three service-linked roles up front (ECS, Elastic Load Balancing, Application
Auto Scaling for ECS), so a fresh account never races, and says to wait at least 2 minutes before `create`
if any were just made. `check` lists all three. Tested against the fake AWS: created when missing, skipped
when present. In this account all three now exist and are about 12 minutes old, so a retry should go through.

**Retry blocked by my helper (14:58):** `delete` reported the service gone and 0 load balancers; `roles`
skipped all five roles (they exist); `create` then failed: "Unable to Start a service that is still
Draining." Cause: the helper decided "gone" from list-services, which stops listing a service as soon as its
deletion starts, while the name stays reserved until the drain finishes. Fabian: "I feel like we're going
round in circles so let's get this finished please."

**Fix:** a `service_state()` asked by name through describe-services (ACTIVE, DRAINING, INACTIVE or MISSING).
`delete` now waits until INACTIVE or MISSING; `create` waits out a drain by itself before creating, so one
command does it. Tested against the fake AWS: create, delete (waits for the drain), and create run
straight after a delete (waits, then creates). `create` still uses the image already in ECR for commit
3c123b2, so this change is deliberately not committed until after the service is up.

**It came up (21/10, 15:00 to 15:19).** `create` waited out the drain, created the service from image
3c123b2ce367, got the URL (a random host under ecs.eu-north-1.on.aws), set MCP_ALLOWED_HOSTS to it, which
starts a second, rolling deployment, then waited for /health and /mcp. Fabian asked whether it was really
waiting rather than failing silently as last time; checked from a second window: `status` showed 0 of 1
tasks and 2 deployments in progress (the first plus the host update), the service events showed "has
started 1 tasks" at 15:06:15 and "registered 1 targets" at 15:06:55, and there were no stopped tasks. So
the role race was gone; the wait was the load balancer's 5 healthy checks 30 s apart. `create` finished
after about 7 to 8 minutes in all.

Against the live URL, from the laptop:
- `python tools\check_http.py --url https://<host>`: **10 of 10 passed** (in --url mode there is no
  self-start check, so 10, not 11). /health ok in sample mode; initialize with the right host; another host
  refused (404); a foreign browser origin 403, claude.ai allowed; stateless tool call; search returns 5
  ranked results; get_profile; the 21-result guard; 10 searches at once all succeed, 10/10 in 1.4 s, each
  waiting 0.9 to 1.3 s (the one-at-a-time lock queuing them, as designed).
- `python tools\inspector_check.py --http https://<host>/mcp`: **12 of 12 passed** (Inspector 2.9.0 over
  Streamable HTTP): exactly the three read-only tools, --strict clean, the jd://{role} resource, the
  shortlist_for_role prompt, list_roles in sample mode, search, both guards, get_profile, the advert.

Not yet exercised at this point: the claude.ai custom connector; the task count staying at 1 after the
burst (not re-read since); a teardown and recreate from the fixed helper end to end (delete was run once,
before the drain fix); what the spend looks like after a day.

**The claude.ai connector (21/10, 15:40).** Fabian asked about authentication first; kept authless as the
build guide recommended (sample data that is already public, read-only tools, no paid API, cost capped at
one task; OAuth would protect nothing and add a sign-in an interviewer doesn't have). He then asked whether
the test wasn't already covered by the Part 2 integration: no, the desktop app's local server has the same
tool names, so the test was done in claude.ai, where only the remote server exists. Added as a custom
connector "candidate-rag (AWS sample)" with the URL ending /mcp, OAuth fields empty. **No sign-in was
asked for.** The prompt "Use candidate-rag to list the roles, then find the 3 best candidates for
semi-senior" listed both roles, searched the 20-person semi-senior pool, read the profiles and put forward
ids 10, 21 and 11 (the same top three as check_http and the Inspector), with 13, 18 and 16 as near misses,
and said the server is running in sample mode. Separately, from a Claude session in Anthropic's cloud
(not the laptop), the same connector's list_roles returned mode "sample" and both roles: the server
answers from outside Fabian's machine.

`status` afterwards: ACTIVE, running tasks 1 of desired 1, 0 deployments in progress, /health 200
sample mode, /mcp answers. The burst of 10 searches did not add a task.

The live URL is kept out of the repo on purpose (it would invite traffic to an authless endpoint); it is
given to an interviewer directly.

**Step 4 status:** everything identified is done. Not exercised: a full delete and recreate with the fixed
helper (delete was run once, before the drain fix); the spend after a day; the CloudWatch logs (Step 5).

## Step 5: CloudWatch logs (21/10)

Fabian: "I want to finish everything today."

Definition of done for this step: the logs are kept 14 days, not forever (the default, a slow silent cost);
the server's lines reach CloudWatch; what someone searches for never does; one saved Logs Insights query.

**New `logs` command in aws_deploy.py.** Sets 14-day retention on the service's log group and on any left by
an earlier service of the same name (the first attempt's group, ending -be17, is still there). Then a marker
check: a made-up word ("canary" plus 10 random hex characters) is sent in four calls to the live URL: a normal
search, a query over the 500-character limit (a validation error, which repeats the input in its message), an
unknown role and an unknown candidate id (our refusals name what was sent, so the caller can correct it). It
waits until the normal search's own log line ("search_candidates role=semi-senior n=5 ...") has reached
CloudWatch, which proves the logs arrive at all, then searches the group for the marker: any hit is a FAIL and
the lines are printed. Last, it saves the Logs Insights query "candidate-rag-mcp/activity" (every line except
the load balancer's health checks, newest first) and runs it once over the last hour.

**What the check found before going near AWS (tested against a local server, with a stand-in for the AWS CLI
that reads the server's real log file):** 2 lines contained the marker. The SDK logs every tool refusal at INFO
with its message: `Tool 'search_candidates' failed: "... Unknown role 'canary...'"` and `Tool 'get_profile'
failed: "... No candidate with id 'canary...'"`. The search text itself was not in either, and the over-long
query was logged as field names only (the SDK does that for validation errors: `rejected arguments:
['query']`). But a caller can type anything into role or candidate id, so the claim "nothing the caller sends
is logged" was false. A failed resource read logs the URI the caller asked for, the same way.

**Fix at the source:** a logging filter on the SDK's `mcp.server.mcpserver.server` logger in mcp_server.py.
Refusals are still logged as events ("Tool 'get_profile' refused the call (its message is not logged)";
"Resource read refused (URI and message not logged)"); the caller still gets the full message. New test
`test_refused_calls_never_put_the_callers_text_in_the_log` sends the marker through all four routes over HTTP
and checks the log: passes with the filter, fails with it removed (checked). Tests: 42. The `logs` command rerun
against the fixed local server: no line contains the marker; the query shows the refusals without the text.

**New `update` command**, because the fix is in the image: rolls the running service onto the image for the
current commit, copying the current container settings (port, MCP_ALLOWED_HOSTS, log configuration) with only
the image changed, and waits until that image is the only one running and /health and /mcp answer. Same charge:
one task. Tested against the fake AWS: update, then a rerun skips; the log configuration and host survive.
Step 6's pipeline will use it.

Logging cost at this volume: the health checks are most of the lines (a few a minute, about 100 bytes each),
so well under 1 MB a day; ingestion and storage come to pennies a month.

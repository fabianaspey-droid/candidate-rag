# Results

Candidate matching over a real pool of 145 LinkedIn profiles sourced for two live accountancy-practice roles (credit controller; semi-senior accountant). All figures are counts.

**How to read these numbers**

- The answer key is 20 candidates per role, sampled at random within each rule-based verdict group and labelled strong / possible / no by an LLM reviewer (Claude, in the build session) from the profile text alone, before any retrieval output existed and without seeing the rule-based verdicts (it had seen 4 of the 40 in earlier check output). It is not a human-labelled set. The rule-based shortlist it is compared with was reviewed by the recruiter before the original demo.
- The re-ranker is also Claude (Haiku 5.5), so agreement between the two is partly circular.
- Hybrid search was added after the first retrieval run showed the embedding model missing qualified semi-seniors, and was chosen as the input to re-ranking after seeing all four methods.
- Small samples: a difference of one or two candidates is noise.

## Credit controller

Pool after hard filters: 138. Answer key: 20 labelled (7 strong, 7 possible, 6 no). Rule-based strong in the pool: 50.

### Retrieval: top 20 per method

| Method | Answer-key strong found | Answer-key 'no' let in | Rule-based strong found |
|---|---|---|---|
| Random 20 (expected) | 1.0 of 7 | - | 7.2 of 50 |
| sections_rrf | 5 of 7 | 0 | 18 of 50 |
| whole_ad | 5 of 7 | 0 | 18 of 50 |
| keyword_bm25 | 3 of 7 | 0 | 6 of 50 |
| hybrid_rrf (fed to re-ranking) | 5 of 7 | 0 | 14 of 50 |

### Re-ranking (claude-haiku-5-5, prompt v2, the hybrid_rrf top 20)

| Measure | Count |
|---|---|
| Marked strong by Claude | 12 |
| ...of which in the answer key | 4 |
| ...answer key agrees strong (precision) | 4 of 4 |
| ...answer key says possible | 0 |
| ...answer key says no (false positives) | 0 |
| Answer-key strong that reached Claude | 5 |
| ...kept strong by Claude | 4 |
| Evidence quotes verified word for word | 19 of 20 |
| Downgraded by the evidence guard | 1 |
| Cost of the run | $0.0038 (6048 tokens in, 6368 out) |

### Where the pipeline and the rules disagree

- Claude strong, rules not strong: 1 (answer key: 0 strong, 0 no, 1 not in the sample)
- Rules strong, Claude not strong: 39 (not retrieved: 36; retrieved but Claude possible: 3; retrieved but Claude reject: 0)

### Run-to-run variation (re-ranking run twice on the same 20)

| | Run 1 | Run 2 |
|---|---|---|
| Marked strong | 12 | 13 |
| Answer key agrees strong | 4 of 4 | 5 of 5 |
| False positives | 0 | 0 |
| Cost | $0.0038 | $0.0034 |

Tier changed for 5 of 20 candidates between runs; score changed by 5.8 points on average (largest 20).

### Step 7: the agent (Claude chooses its own searches)

| Measure | Agent | Re-ranking (run 1) |
|---|---|---|
| Searches made | 7 | 4 fixed section queries + BM25 |
| Candidates seen | 63 | 20 |
| Answer-key strong seen | 6 of 7 | 5 of 7 |
| Marked strong | 17 | 12 |
| Answer key agrees strong | 3 of 3 | 4 of 4 |
| False positives | 0 | 0 |
| Cost | $0.0160 (5 turns) | $0.0038 |

Searches the agent chose: "credit controller business-to-business credit control professional services partners billing WIP"; "credit control experience aged debt reporting chasing overdue invoices"; "accounts receivable credit collector sales ledger debtors"; "accounts assistant eager to step into first finance role, communication, organised"; "credit controller accountancy firm chartered accountants billing queries partners WIP recovery"; "CICM qualified credit management professional"; "credit controller ACCA or AAT qualified finance professional debtor management"

## Semi senior

Pool after hard filters: 138. Answer key: 20 labelled (8 strong, 5 possible, 7 no). Rule-based strong in the pool: 5.

### Retrieval: top 20 per method

| Method | Answer-key strong found | Answer-key 'no' let in | Rule-based strong found |
|---|---|---|---|
| Random 20 (expected) | 1.2 of 8 | - | 0.7 of 5 |
| sections_rrf | 3 of 8 | 0 | 2 of 5 |
| whole_ad | 4 of 8 | 0 | 3 of 5 |
| keyword_bm25 | 5 of 8 | 0 | 4 of 5 |
| hybrid_rrf (fed to re-ranking) | 6 of 8 | 0 | 4 of 5 |

### Re-ranking (claude-haiku-5-5, prompt v2, the hybrid_rrf top 20)

| Measure | Count |
|---|---|
| Marked strong by Claude | 5 |
| ...of which in the answer key | 4 |
| ...answer key agrees strong (precision) | 4 of 4 |
| ...answer key says possible | 0 |
| ...answer key says no (false positives) | 0 |
| Answer-key strong that reached Claude | 6 |
| ...kept strong by Claude | 4 |
| Evidence quotes verified word for word | 20 of 20 |
| Downgraded by the evidence guard | 0 |
| Cost of the run | $0.0037 (6206 tokens in, 6249 out) |

### Where the pipeline and the rules disagree

- Claude strong, rules not strong: 2 (answer key: 1 strong, 0 no, 1 not in the sample)
- Rules strong, Claude not strong: 2 (not retrieved: 1; retrieved but Claude possible: 1; retrieved but Claude reject: 0)

### Run-to-run variation (re-ranking run twice on the same 20)

| | Run 1 | Run 2 |
|---|---|---|
| Marked strong | 5 | 5 |
| Answer key agrees strong | 4 of 4 | 3 of 3 |
| False positives | 0 | 0 |
| Cost | $0.0037 | $0.0035 |

Tier changed for 2 of 20 candidates between runs; score changed by 5.7 points on average (largest 12).

### Step 7: the agent (Claude chooses its own searches)

| Measure | Agent | Re-ranking (run 1) |
|---|---|---|
| Searches made | 7 | 4 fixed section queries + BM25 |
| Candidates seen | 40 | 20 |
| Answer-key strong seen | 8 of 8 | 6 of 8 |
| Marked strong | 7 | 5 |
| Answer key agrees strong | 5 of 5 | 4 of 4 |
| False positives | 0 | 0 |
| Cost | $0.0116 (5 turns) | $0.0037 |

Searches the agent chose: "Semi-senior accountant AAT qualified accounts preparation VAT personal tax Xero"; "studying towards ACA or ACCA accounts preparation bookkeeping VAT returns"; "accountant mentoring trainees management accounts personal tax returns"; "semi senior accountant with VAT and personal tax experience" (filters {'aat_qualified': True}); "studying ACCA or ACA accountant with accounts preparation" (filters {'aca_acca_in_progress': True}); "accountant with ACA or ACCA qualification accounts preparation" (filters {'aca_acca_held': True}); "bookkeeper Xero QuickBooks Sage client bookkeeping VAT returns accounts assistant"

## How the rule-based verdicts compare with the answer key

| Role | Rules strong / key strong | Rules strong / key no | Rules reject / key strong |
|---|---|---|---|
| credit-controller | 7 | 0 | 0 |
| semi-senior | 5 | 0 | 0 |

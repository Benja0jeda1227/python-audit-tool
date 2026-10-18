# Fictional DEMO samples

**Every record, company, review label, and evidence excerpt in this directory is fictitious. These files demonstrate formats and workflows; they are not verified business research.**

Only this small selection is intended for publication. Working datasets, manual notes, generated reports, and identity/recovery stores remain local and are excluded from Git.

| File | Records | Purpose |
|---|---:|---|
| [discovery/candidates_DEMO.csv](discovery/candidates_DEMO.csv) | 3 | One fictional discovery observation per configured country; 14 columns |
| [review/candidate_review_DEMO.csv](review/candidate_review_DEMO.csv) | 3 | Matching pending-review records with persistent demo IDs; 20 columns |
| [review_notes_DEMO.csv](review_notes_DEMO.csv) | 3 | Fictional annotations for the optional triage step |
| [audit/audit_results_DEMO.csv](audit/audit_results_DEMO.csv) | 5 | Candidate-level output from the separate built-in audit fixtures |
| [audit/evidence_snippets_DEMO.csv](audit/evidence_snippets_DEMO.csv) | 2 | Selected excerpts from the same fictional audit fixtures |

The discovery/review pair uses `example.com`. The independent audit fixtures use `demo.invalid` and `TEST-` IDs: **the audit files are not an audit of the discovery sample**. Labels such as `Eligible`, `Supported`, and manufacturing claims in those audit fixtures are invented inputs, not conclusions about real companies. No published sample contains real contact details, private review history, or API credentials.

The discovery/review snapshots were generated offline with `research.py --demo --results-per-query 1`. Their research notes were replaced with explicit publication-safe fictional labels. Audit snapshots came from `website_audit_v2_1.py --demo`; only two of its ten evidence excerpts are retained. Generated UUIDs and timestamps can differ between fresh runs.

## Run the samples

From the repository root after installing the requirements:

```powershell
.\.venv\Scripts\python.exe pipeline_v2_2.py --demo --input examples/discovery/candidates_DEMO.csv --review examples/review/candidate_review_DEMO.csv --out output/examples_review_DEMO
.\.venv\Scripts\python.exe triage.py --input examples/discovery/candidates_DEMO.csv --notes examples/review_notes_DEMO.csv --outdir output/triage_DEMO
.\.venv\Scripts\python.exe website_audit_v2_1.py --demo
```

These commands require no credentials and make no network requests. The first command integrates the three matching records; repeating it preserves their IDs without adding candidates. The second demonstrates preliminary annotations. The third uses the independent five-record audit fixtures and creates the complete seven-report bundle under `output/website_audit_v2_1_DEMO/`.

Outputs are written to ignored local directories. Review or experiment with those copies while keeping the published samples unchanged. Use `--demo` when integrating these files; the real integration mode rejects demo identities. Do not send the sample URLs to a live auditor.

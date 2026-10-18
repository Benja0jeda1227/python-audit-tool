# Multilingual Company Research Pipeline

A Python portfolio project for multilingual business research: discover candidate pages, maintain a persistent review queue, and audit website evidence without replacing human judgment.

The current search configuration targets dairy manufacturers in **France, the United States, and Chile**, using French, English, and Spanish queries. The v2.2 pipeline uses the v2.1 audit engine; the stabilized integration reports revision `2.2.1`.

Search results are leads, not verified companies. Manufacturing, location, eligibility, and group relationships remain research decisions supported by documented sources.

## Features

- Tavily discovery with configurable query/result limits and a fictional offline demo.
- CSV transformation that preserves provenance, manual review fields, exclusions, and additional review columns.
- Persistent candidate IDs and historical URL aliases; ambiguous matches are reported for human review.
- Field-level reconciliation between manual and integrated copies, with unresolved alternatives retained.
- Shared URL validation, public-destination checks, redirect handling, and robots-aware HTML auditing.
- Evidence excerpts, role/product/location hints, and prioritized follow-up reports.
- Spreadsheet formula protection, recognizable-secret redaction, and recoverable exports with integrity checks.

## Architecture

```text
Discovery → candidate CSV → persistent integration → manual review → optional website audit
```

`research.py` automatically invokes integration after saving discovery results. `pipeline_v2_2.py` integrates existing CSVs and enables auditing only when `--audit` is supplied.

| Component | Responsibility |
|---|---|
| `research.py` | Multilingual discovery and discovery exports |
| `candidate_integration.py` | Schema validation, identity, deduplication, reconciliation, and provenance |
| `integration_store.py` | Writer locks, versioned generations, checksums, and recovery |
| `url_policy.py` / `safe_http.py` | URL comparison keys, safe export values, DNS checks, and HTTP transport |
| `website_audit_v2_1.py` / `website_audit.py` | Evidence auditing and shared fetching/CSV infrastructure |
| `triage.py` | Optional preliminary classification using `review_notes.csv` |
| `tests/` | Fixtures, regression tests, and the offline test runner |
| `examples/` | Small, explicitly fictional discovery, review, triage, and audit samples |
| `OPERATIONS.md` | Public guide to identity, manual conflicts, migration, and recovery |

Earlier audit scripts remain in the repository for compatibility.

## Installation and configuration

Run commands from the project root. Python **3.10+** is the intended baseline; the current validation environment is **Windows / Python 3.15.0**. Other environments have not been certified.

Windows PowerShell:

```powershell
py -m venv .venv
.\.venv\Scripts\python.exe -m pip install -r requirements.txt
```

Using the virtual environment's interpreter directly avoids activation-policy issues. The requirements are `tavily-python` and `python-dotenv`; integration, auditing, and storage otherwise use the Python standard library.

An API key is needed only for live discovery. If no local configuration exists:

```powershell
if (!(Test-Path .env)) { Copy-Item .env.example .env }
```

Edit the local `.env` and set:

```dotenv
TAVILY_API_KEY=replace_with_your_key
```

Never commit this file or a real key. Offline demos and tests need no credentials.

## Run the project

### 1. Try the offline demos

```powershell
.\.venv\Scripts\python.exe research.py --demo
.\.venv\Scripts\python.exe website_audit_v2_1.py --demo
```

The first command creates nine fictional discovery observations by default and integrates them into `output/integration_v2_2_DEMO/`. Existing manual review rows are also retained in that demo queue, so its total row count may be larger.

A clean checkout contains no private `input/` or `output/` datasets. The demo creates its output directories and works without a manual review file. See [fictional samples and reproducible commands](examples/README.md) for a smaller three-record example.

The second command audits a separate built-in set of five fictional candidates and writes `output/website_audit_v2_1_DEMO/`. Neither command makes network requests. Demo records are not business evidence.

### 2. Discover candidates with Tavily

**These commands call an external service and may consume provider credits.**

```powershell
.\.venv\Scripts\python.exe research.py
```

Defaults: one query per country, up to eight results per query, and `search_depth="basic"`. To use both configured queries per country:

```powershell
.\.venv\Scripts\python.exe research.py --queries-per-country 2 --results-per-query 5
```

Discovery retains observations from different queries/countries. Their count can exceed the number of distinct URLs or candidate identities. Search titles and result URLs are never promoted automatically to company names or official websites.

### 3. Integrate and review existing results

Integration alone is offline and does not call Tavily:

```powershell
.\.venv\Scripts\python.exe pipeline_v2_2.py
```

Defaults are `output/candidates.csv`, `input/candidate_review.csv`, and `output/integration_v2_2/`. Custom paths are supported:

```powershell
.\.venv\Scripts\python.exe pipeline_v2_2.py --demo --input examples/discovery/candidates_DEMO.csv --review examples/review/candidate_review_DEMO.csv --out output/examples_review_DEMO
```

This command uses fictional data and must retain `--demo`. Real integration needs discovery results from a prior live run or your own compatible CSV. The manual input is optional on the first run; private review files and identity state are not distributed with the repository.

Review the integrated CSV or the manual input, preserving candidate IDs. Record the official site, products, manufacturing/location evidence, review date, and reasons for inclusion or exclusion. Keep unsupported candidates pending. Removing a row or column does not authorize deleting its historical information.

Changes to different fields are reconciled. Conflicting changes to the same field retain both alternatives in `integration_report.json`. Both `research.py` and `pipeline_v2_2.py` warn and exit with code `2` when manual conflicts remain; discovery results stay saved, and the pipeline postpones auditing. Successful runs exit with code `0`. Set the intended value in both review copies to resolve a field conflict. Advanced reviewed identity mappings are available through `--identity-map`; see the operational notes below.

### 4. Audit supplied website sources

**This command makes HTTP requests to supplied sources, but does not call Tavily.**

```powershell
.\.venv\Scripts\python.exe pipeline_v2_2.py --audit
```

The audit consumes the confirmed integration snapshot and writes `output/integration_v2_2/audit/`. Candidates without valid supplied sources receive `NO_VALID_SOURCES`; the program does not invent a website to fetch. `--demo --audit` is intentionally rejected.

Use `--help` on each script to inspect its supported arguments.

### 5. Optional preliminary triage

`triage.py` requires a separate notes CSV; a clean checkout supplies only fictional example notes:

```powershell
.\.venv\Scripts\python.exe triage.py --input examples/discovery/candidates_DEMO.csv --notes examples/review_notes_DEMO.csv --outdir output/triage_DEMO
```

For real work, supply your own `--notes` file or keep private annotations in the ignored root `review_notes.csv`. Triage labels are preliminary. This legacy helper writes its reports directly; it does not use the recoverable export store described below. Use a separate output directory when preserving previous triage reports.

## Outputs and example

| Output | Contents |
|---|---|
| `output/candidates.csv` / `summary.txt` | Discovery observations and counts; demo equivalents use `_DEMO` |
| `output/integration_v2_2/candidate_review.csv` | Review-compatible CSV: 20 standard columns, plus preserved custom columns |
| `output/integration_v2_2/integration_report.json` | New/existing/ambiguous/rejected events, provenance, conflicts, and URL issues |
| `audit_results.csv` / `quality_comparison.csv` | Candidate-level machine hints alongside unchanged manual labels |
| `page_checks.csv` / `evidence_snippets.csv` | Fetch status, original/final URLs, and attributable text excerpts |
| `alerts_detailed.csv` / `manual_followups.csv` / `audit_summary.md` | Alerts, recommended follow-ups, and run summary |

Illustrative integrated demo row; fields are omitted and the generated UUID varies:

| Candidate ID | Target Country | Company / Group | Official Website | Project Eligibility |
|---|---|---|---|---|
| `DEMO-N-<uuid>` | France | *(empty)* | *(empty)* | `Needs review` |

The original search URL remains provenance. A readable page or manufacturing phrase is a machine hint, not independent verification.

Published CSV snapshots are limited to [examples/](examples/README.md). The three-row discovery/review pair and the separate five-row audit fixture set are entirely fictional; the audit samples do not verify the discovery samples. Runtime outputs in the table above remain local.

## Integrity, export safety, and recovery

Existing IDs are preserved; new `N-…` IDs are UUID-based and independent of mutable names/URLs. Discovery IDs such as `C-001` are not persistent identity keys. Historical aliases support matching across runs; shared domains/groups and contradictory attributes require review.

URL comparison preserves meaningful query parameters. Validation distinguishes allowed syntax from reachability and factual verification. The HTTP transport checks resolved addresses before connecting, validates redirects, and attributes evidence to the final URL.

CSV projections neutralize potential formulas. Canonical non-sensitive values remain in internal state, while recognizable credentials are redacted even in rejected records. Invalid review URLs are retained internally for correction but excluded from audit sources.

Each integration output directory contains a private `.integration/` store. Discovery and audit export snapshots use `.exports/` under their output directories. Complete generations are flushed, checked with SHA-256, and committed through an atomic `CURRENT` pointer before compatibility copies are published.

After a permission error or interruption, close applications locking the files and repeat the command. Recovery completes committed exports; unexpected external edits or corrupt state stop processing instead of being overwritten. Keep state directories with their exports when backing up or moving the project. Do not delete `CURRENT`, manifests, or generations to force a reset.

The first stabilized run migrates legacy v2.2 CSV/report outputs without renumbering candidates. Ambiguous historical differences remain conflicts. See the [review, identity, and recovery guide](OPERATIONS.md) for conflict resolution, reviewed identity mappings, and operational details.

## Automated tests

```powershell
.\.venv\Scripts\python.exe -B tests/run_offline.py
```

Current local result: **116 tests passing** on Windows / Python 3.15.0. The runner blocks real sockets, DNS, and dotenv loading. Tests use fictional data, temporary directories, and mocked search/HTTP services.

Coverage includes identity migration, duplicate/ambiguous matches, manual conflicts, idempotence, URL safety, formula protection, redirects, write failures, corruption, recovery, and the complete discovery-to-audit workflow.

## Limitations and publication hygiene

- Search coverage is limited to the configured countries, languages, and queries. There is no guarantee of exhaustive coverage or a fixed number of verified manufacturers.
- HTML auditing does not render JavaScript. Access restrictions and missing text do not invalidate documented manual research.
- Live-service interoperability and a broader Python/OS matrix have not been validated. Environment proxies are disabled; DNS timing depends on the operating system.
- Root export files are not replaced simultaneously. Consumers should use the pipeline's verified snapshots and recovery protocol.
- Generations accumulate on disk. Independent backups remain necessary; hardware failure and network/cloud filesystem semantics are outside the recovery guarantee.
- Redaction recognizes known patterns, not all confidential information in free text. Candidate identities without relatable evidence still require human review.

The public repository contains source code, requirements, automated tests and synthetic fixtures, the README and operations guide, the `.env.example` placeholder template, and explicitly fictional examples. Internal working reports in `docs/` and the unfinished portfolio draft are local files and are not distributed.

`.gitignore` excludes root `input/` and `output/`, root manual notes and identity mappings, internal reports/drafts, environment files except `.env.example`, local configuration, virtual environments, caches, logs, temporary files, backups, and private state/export stores. Keep custom private datasets under the ignored directories; arbitrary CSV paths elsewhere are not automatically private. `examples/` and synthetic `tests/fixtures/` remain publishable.

Previously tracked working datasets, exports, and internal drafts/reports have been removed from the proposed Git tree and excluded from local history references. Local working copies remain on disk, ignored by Git. Private recovery backups of the original Git state are stored outside the repository; never publish those backups.

The portfolio baseline has been published to [python-audit-tool](https://github.com/Benja0jeda1227/python-audit-tool) (`portfolio` remote). The former [Projects](https://github.com/Benja0jeda1227/Projects) remote is a separate repository: the local cleanup does not establish whether historical copies remain there, in forks, or in caches. Fetching or pulling older branches can reintroduce removed history; review them before bringing them into this repository.

**Development approach.**
This project was developed with extensive AI-assisted coding using OpenAI Codex. My contributions include defining project requirements, directing development iterations, testing outputs, reviewing results, and identifying areas for improvement. Much of the code implementation was generated with AI assistance.
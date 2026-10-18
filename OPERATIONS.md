# Review, identity, and recovery guide

This guide covers the persistent v2.2 integration and the v2.1 audit engine. See the [README](README.md) for installation, commands, and limitations, and [examples](examples/README.md) for fictional offline datasets.

## CSV contracts and candidate identity

Discovery exports contain 14 columns; review exports contain 20 standard columns and preserve additional manual columns. The [discovery sample](examples/discovery/candidates_DEMO.csv) and [review sample](examples/review/candidate_review_DEMO.csv) show the supported headers. Keep candidate IDs when editing review files.

Existing review IDs are retained. New canonical IDs use UUIDs prefixed with `N-` or `DEMO-N-`; names, sites, and other editable attributes do not determine those IDs. Discovery observation IDs such as `C-001` can be recycled and are not canonical company identities.

Original/official URLs and reviewed alternate IDs are maintained as historical aliases. Shared domains or groups do not automatically merge candidates. Contradictory names, countries, or sites require review. If a candidate changes every identifiable attribute without retaining an ID or historical alias, the system cannot safely infer its identity.

The first stabilized integration can migrate legacy v2.2 CSV/report outputs without renumbering existing candidates. It does not modify the manual input file. Conflicting historical values require review; duplicate or malformed IDs stop processing. Real integration rejects `DEMO-` and `TEST-` identities.

## Reconcile manual changes

The manual input, previous snapshot, and integrated CSV participate in field-level reconciliation:

| Situation | Behavior |
|---|---|
| A field changes in only one copy | Keep that change, including an explicit cleared cell |
| Both copies make the same change | Accept it once |
| Both copies change a field differently | Keep the base value and record both alternatives |
| A row or column disappears | Preserve its historical information |
| Discovery matches an existing candidate | Retain provenance without replacing manual judgments |
| Discovery creates a new candidate | Set `Needs review`; leave unsupported business facts empty |

Unresolved conflicts appear in `integration_report.json`; the pipeline returns exit code `2` and postpones auditing. To resolve a field conflict, put the intended value in both review copies, preserving the candidate ID, and repeat integration. Removing the conflicting row does not resolve its retained alternatives. Do not edit internal state JSON directly.

An ambiguous reviewed identity can be resolved with an explicit alias map after human verification. For example, a private `input/reviewed_identity_map.json` could contain:

```json
{"REVIEW-ALIAS": "N-canonical-existing-id"}
```

```powershell
.\.venv\Scripts\python.exe pipeline_v2_2.py --identity-map input/reviewed_identity_map.json
```

Replace these illustrative IDs with actual reviewed identities. The destination must already exist; mappings cannot merge two established canonical identities. Keep one manual row for the resolved identity, and do not use recycled discovery IDs as mapping sources. The decision becomes a persistent alias. Identity maps remain private.

## URLs and audit evidence

URL syntax, network reachability, and business verification are different checks. A readable page is not proof that a company meets the research criteria.

HTTP/HTTPS URLs are validated across source fields. Comparison keys normalize host/IDNA, `www`, default ports, trailing slashes, and fragments while retaining meaningful query parameters. HTTP and HTTPS can remain distinct. Invalid optional review URLs are reported and retained internally for correction but excluded from audit sources.

The transport validates DNS results before connecting, pins connections to checked public addresses, checks TLS hostnames, and revalidates redirect destinations. It rejects private/local destinations, unsafe schemes, recognized URL credentials, and HTTPS-to-HTTP downgrades. Environment proxies are disabled. Redirect targets must satisfy robots rules; evidence is attributed to the final URL rather than inheriting the initial site's authority.

Connection, timeout, HTTP, protocol, content-type, size, and decoding errors produce per-page statuses. Inaccessible or unsuitable robots responses do not grant access. Audit hints do not overwrite manual eligibility or verification labels.

## Export and recovery protocol

Each integration output directory has a private `.integration/` store. Discovery and v2.1 audit exports use `.exports/<type>/`. A writer lock coordinates pipeline processes. Export publication follows these steps:

1. Write and flush a complete new generation.
2. Record and verify SHA-256 checksums.
3. Atomically replace `CURRENT` to commit that generation.
4. Publish the compatibility CSV/report copies.

A failure before commitment leaves the previous committed generation available. After commitment, rerunning the command completes interrupted compatibility exports. The pipeline recovers discovery exports before reading them, and auditing consumes the verified integration snapshot. Root files in a multi-file bundle are not replaced simultaneously; external readers must respect this recovery protocol.

If an application locks an output file, close it and rerun the same command. If an output was edited externally after a failure, processing stops rather than overwriting the edit. Save the edited copy separately and reconcile it with the committed data before continuing. Corrupt checksums, missing `CURRENT` in an initialized store, or incomplete state also stop processing. Restore a complete backup; do not delete manifests, hashes, or pointers to force a reset.

Keep CSVs, reports, and their state/export stores together when moving or backing up work. Generations are retained and consume disk space; automatic retention cleanup is not implemented. Local recovery does not replace independent backups or guarantee durability on every network/cloud filesystem.

CSV projections protect potential spreadsheet formulas; non-sensitive canonical values remain in internal state. Recognizable secrets are redacted, including rejected records, but arbitrary confidential free text may escape pattern-based redaction. Keep working data under ignored `input/` and `output/` directories and review custom export paths before publication.

The optional legacy `triage.py` helper writes directly to its output directory and does not use this recovery store. Use a separate output directory to preserve earlier triage reports.

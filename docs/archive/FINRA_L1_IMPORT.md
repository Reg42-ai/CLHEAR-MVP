> Historical research only. This document is not an active corpus scope, operational runbook, permission grant or acceptance record. Current requirements are in [L1_PUBLISHER_SCOPE.md](../L1_PUBLISHER_SCOPE.md).

# Verifying the first FINRA L1 import

> Historical local verification record. The commands below are not the daily
> operating path. Current work must use CLHEAR's `AdapterRunRequested` worker
> and its registered fleet. Protected acquisition now requires explicit source
> permission evidence before fetching; old local snapshots do not supply it.
> See [L1_REVIEW_WORKSPACE.md](L1_REVIEW_WORKSPACE.md) for the current controls
> and unresolved acceptance requirements.

This procedure covers the five individual rules currently registered in CLHEAR:
2210, 3110, 2111, 3310 and 4511. It does not certify the complete FINRA rulebook.
The `finra/rulebook` landing page is an index, not the text of those rules.

## Acceptance checks

- Preserve the original downloaded HTML bytes and SHA-256.
- Select the official rule heading and rule body, excluding site navigation.
- Match the complete ordered text after whitespace normalization. Words,
  punctuation, repeated passages, supplementary material and history/notes must
  survive. This is stricter than substring coverage.
- Preserve the rule's paragraph numbering, including uppercase and roman
  subparagraphs, with individually addressable clause records.
- Read the saved database rows back and compare their text, references, parent
  links, hashes, order and canonical character spans against the parsed snapshot.
- Keep FINRA's existing `derived_only` classification. Internal text is retained;
  public text flags remain false and original artifacts use `restricted/`.

The original HTML is byte-exact. Structured text normalizes HTML whitespace;
it is not a byte-for-byte copy of the publisher's markup or page layout.
Retrieval timestamps do not establish a legal effective date.

## Offline audit and import

Obtain each selected rule directly from its registered FINRA URL. Save it as
`<rule>.html`. Use a fresh acquisition, not CLHEAR's persistent HTTP fixture
cache or last-good fallback. If supplied, `acquisition.json` must be a list of
records containing `rule`, `url`, `final_url`, `retrieved_at` (with timezone),
`sha256` and `bytes`. The command verifies that the manifest matches the files;
the manifest alone does not authenticate their origin.

Use a copy of an existing corpus with the current schema. The command never
creates or migrates a database and audits without writes by default:

```sh
python3 scripts/verify_finra_import.py \
  --database /absolute/path/corpus-copy.db \
  --artifact-dir /absolute/path/finra-originals \
  --report /absolute/path/before.json

python3 scripts/verify_finra_import.py \
  --database /absolute/path/corpus-copy.db \
  --artifact-dir /absolute/path/finra-originals \
  --report /absolute/path/import.json --import

python3 scripts/verify_finra_import.py \
  --database /absolute/path/corpus-copy.db \
  --artifact-dir /absolute/path/finra-originals \
  --report /absolute/path/after.json
```

`--rules 2210 3110` selects a smaller subset. Exit status zero means all selected
rules passed the reported checks; inspect artifact verification and provenance
fields as well. An absent rule, damaged text, wrong reference or mismatched hash
fails the audit. Repeating a valid import leaves its versions unchanged.
Re-imports that need repair use a new version label, preserving old row IDs.

Imports validate all supplied snapshots before the first database mutation.
Each rule is then persisted in its own transaction; a later failure is reported
and does not roll back earlier successful rules. Re-run the audit to identify
the remaining work.

This path calls no model, skips the global embedding index and does not relay
events or run downstream layers. Normal L1 lineage, annotations, search units,
citations and outbox events are recorded locally by the ingestion pipeline.

## Deployment boundary observed on 2026-09-15

The public explorer reads `webui/clhear-latest.db` from the platform deployment
bucket. The inspected object was last modified on 2026-09-13 at 09:22:58 UTC;
it contained only a FINRA landing-page source and no individual FINRA rule
sources. The snapshot passed SQLite integrity checks and had schema version 23.

The deployed L1 worker uses Aurora PostgreSQL with snapshot mode disabled.
Direct PostgreSQL access from this workstation was unavailable; RDS Data API
and ECS Exec were disabled. Consequently the explorer snapshot is evidence of
the explorer's contents, not a verified inventory of the Aurora record.

The offline command produces a local verification candidate, not a deployable
replacement for Aurora. Its archived artifact URIs reference local files.
Before production acceptance, use an authorized execution path to audit/import
the same validated artifacts into Aurora, store originals in the restricted
datalake, and verify that the explorer's published projection matches the
record. No live import or snapshot publication is part of this command.

## Full-rulebook follow-up

The existing registry lists only five individual rules. FINRA's landing and
expanded HTML indexes differ and include structural headings, reserved entries
and URL aliases. Neither index alone is a completeness denominator. A full-book
import needs a reconciled inventory with canonical page identity, hierarchy and
current/historical status, or access to the production FINRA rulebook API.
Never use the API's mock dataset as regulatory source text.

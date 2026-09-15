# CLHEAR

CLHEAR maintains a company-independent corpus of financial compliance sources,
with preserved publisher originals, versioned document structure, clause records
and worker-produced verification evidence. Organizations consume that corpus
when constructing compliance programme blueprints.

The current delivery focus is **L1**. L0 coordinates its workers and publishes the
private inspection interface. L2–L8 remain held until L1 acceptance. A successful
worker run, deployment or signed-in viewer is not evidence of a complete corpus.

## L1 workflow

A manual request and the daily adapter occurrences use the same persisted cycle:

`L0 request → L1 publisher discovery → freeze inventory → L0 dispatch → L1 source tasks → L1 readback/evaluations → L0 viewer publication`

Source tasks retain permission decisions, attempts, original artifact hashes,
parser identity, measured steps, canonical spans and original locations. All 32
configured adapter lanes remain in scope. Missing artifacts, unsupported
catalogs, incomplete enumeration and permission dependencies remain unresolved.
Manual events never satisfy scheduled-delivery checks.

See [L1 corpus verification](docs/L1_CORPUS_VERIFICATION.md) for the execution
contract, inspection path, current limitations and acceptance criteria.

## Private inspection

The eight-layer overview shows the output structure. L1 exposes **Sources,
Fleet, Evals and Changes**. The reader connects an immutable source version to
its original text, encoded nodes/clauses, worker steps and evaluation findings.
Acquisition permissions, import outcomes and verification are separate states.

Sign-in and content permissions are independent. Original standards require
edition-specific authorized artifacts and processing evidence. Test-origin
records and synthetic cohorts are excluded from production publication.

## Development and deployment

Install pinned dependencies with `pip install -r requirements.lock`. Run
`pytest tests/` for isolated fixture tests. The integration journey lives under
`tests/support/` and accepts only a local disposable test instance.

Use `scripts/preview.py` for a private read-only preview of an L0-published viewer
snapshot. Authentication secrets and API keys have no bundled development
defaults; they must be explicitly supplied by the execution environment.

Changes deploy through the owner-merge GitHub workflow after CI succeeds. The
protected deployment workflow also accepts `operation=verify-l1` to request a
cycle from the currently deployed L0 worker without rebuilding or redeploying.
It verifies all configured schedules, running worker identities and the L1 hold
before submitting the request. The receipt confirms submission only.

Operational discovery, acquisition, migrations, audits and publication belong
to CLHEAR workers. A workstation must not import documents or repair the live
Aurora database. The accepted-release pointer is preserved while evidence is
incomplete; nightly validation requires two genuine 00:00 UTC cycles.

# Build handoff — state and next steps

Read [CLHEAR_HLD.md](CLHEAR_HLD.md) first; it is the source of truth. This file
tells a fresh agent (or human) exactly where the build stands and what to do next.

## Where things stand

- **P0 (L0 rails) is code-complete** on branch `cursor/clhear-p0-l0-rails-b7a1`
  ([PR #1](https://github.com/Reg42-ai/CLHEAR-MVP/pull/1)): outbox+relay, LLM
  gateway with caps, proposals + `/review`, evals harness, exporter, `l0_platform`
  migration, Terraform in `infra/`. `pytest tests/` is green (10 tests, including
  the HLD §9 P0 dummy-fleet done-test). `terraform validate` is green.
- **P0 infra is APPLIED to AWS** (account 730649732189, us-east-1): 32 resources
  — S3 `reg42-clhear-datalake` (Object Lock compliance), SQS `clhear-events`+DLQ,
  disabled EventBridge schedules, IAM roles, CloudWatch dashboard+alarms, SSM
  placeholders, ECS cluster `clhear-cluster`. Terraform state lives in the S3
  backend `reg42-clhear-tfstate` (bootstrapped outside terraform; see
  `infra/versions.tf`), so any agent can `terraform -chdir=infra init` and get
  the live state. `terraform plan` is clean (no drift).
- **The real-AWS rehearsal passed** (`scripts/rehearsal_aws.py`, evidence on
  [PR #1](https://github.com/Reg42-ai/CLHEAR-MVP/pull/1)): event via outbox →
  real SQS → worker, idempotent redelivery, proposal approved via API,
  downstream event through the real queue, snapshot exported + uploaded to the
  datalake under `public-ok/`, `restricted/` read verified DENIED for
  non-worker principals. Queue and DLQ left empty.
- AWS credentials are provided to cloud agents as `AWS_ACCESS_KEY_ID` /
  `AWS_SECRET_ACCESS_KEY` env vars (Cursor Runtime Secrets).
- SSM parameters `/clhear/ANTHROPIC_API_KEY` and `/clhear/DATABASE_URL` are
  still `CHANGEME` placeholders — no key/Aurora DSN was available; set them
  when wiring the real provider/DB (terraform ignores value changes).
- This repo stands in for `reg42-os`/`reg42-infra` (HLD §4), which are not
  accessible; conventions from the HLD are followed so code can migrate later.
  Deviations are marked with `# ARCH:` comments (notably: stand-in maintainer
  auth in `app/clhear/routes.py`, standalone settings/migration runner).

## AWS apply + real rehearsal — DONE (2026-08-21)

Steps 1–3 and 5 of the original plan are complete; re-run the rehearsal any
time with:

    CLHEAR_EVENTS_QUEUE_URL=https://sqs.us-east-1.amazonaws.com/730649732189/clhear-events \
        python scripts/rehearsal_aws.py

Still pending from this phase (not blockers for P1):

- Real SSM parameter values (`/clhear/ANTHROPIC_API_KEY` optional,
  `/clhear/DATABASE_URL` when Aurora is wired).
- ECS service/task + ALB rule: skipped until `worker_image`,
  `existing_vpc_id`, `existing_private_subnet_ids`,
  `existing_alb_listener_arn` are set (reg42-infra values).

## Next: P1–P4 (one PR per phase, HLD §9)

- **P1** — `l1_sources` migration (m0002), adapter contract
  (`app/clhear/l1/adapters/base.py`), fixture adapter, `uk_legislation` (CLML API
  + effects-feed citator sync), `pipeline.py` (hash/version/diff/upsert →
  `change_events` + `SourceChanged`). Done-test: MLRs 2017 fully ingested;
  replayed historical amendment yields correct clause-diff; family auto-contains
  amending SIs.
- **P2** — `eur_lex`, `govinfo_us`, NIST spine; `families.py` citation mining +
  reconciliation; `embeddings.py` (BGE-M3, CPU). Done-test: E2 100% on GDPR;
  semantic search returns MLR reg 27–28 for "customer due diligence".
- **P3** — `restricted_file` importer + BYOL flow; `irs_gov` watcher/fetcher with
  Docling; `guard.py`. Done-test: restricted discipline + BYOL unlock + guard
  blocks paraphrased ISO text.
- **P4** — E1–E7 suites wired to CI + weekly schedule; `/sources` Explorer;
  dashboards; first tagged release `clhear-v0.1.0`.

## Working rules that bite (HLD §8)

- No LLM call outside `platform/gateway.py`; only watcher triage may call it.
- Any path that could emit clause text goes through `clauses_public` or a BYOL
  check — write the tests.
- Adapters must run green offline against recorded fixtures; never hammer
  official endpoints.
- Anything ambiguous: smaller change + `# ARCH:` comment. Never invent scope.

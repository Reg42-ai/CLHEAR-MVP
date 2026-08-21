# Build handoff — state and next steps

Read [CLHEAR_HLD.md](CLHEAR_HLD.md) first; it is the source of truth. This file
tells a fresh agent (or human) exactly where the build stands and what to do next.

## Where things stand

- **P0 (L0 rails) is code-complete** on branch `cursor/clhear-p0-l0-rails-b7a1`
  ([PR #1](https://github.com/Reg42-ai/CLHEAR-MVP/pull/1)): outbox+relay, LLM
  gateway with caps, proposals + `/review`, evals harness, exporter, `l0_platform`
  migration, Terraform in `infra/`. `pytest tests/` is green (10 tests, including
  the HLD §9 P0 dummy-fleet done-test). `terraform validate` is green.
- **Nothing has been applied to AWS yet.** AWS credentials are provided to cloud
  agents as `AWS_ACCESS_KEY_ID` / `AWS_SECRET_ACCESS_KEY` env vars (Cursor
  Runtime Secrets, account 730649732189, us-east-1).
- This repo stands in for `reg42-os`/`reg42-infra` (HLD §4), which are not
  accessible; conventions from the HLD are followed so code can migrate later.
  Deviations are marked with `# ARCH:` comments (notably: stand-in maintainer
  auth in `app/clhear/routes.py`, standalone settings/migration runner).

## Immediate next step — AWS apply + real rehearsal

1. `aws sts get-caller-identity` to verify credentials (expect account 730649732189).
2. `terraform -chdir=infra init && terraform -chdir=infra apply` in us-east-1.
   - Creates: S3 `reg42-clhear-datalake` (**Object Lock compliance mode —
     irreversible; owner has approved creating it**), SQS `clhear-events`+DLQ,
     disabled EventBridge adapter schedules, IAM roles, CloudWatch dashboard +
     alarms, SSM parameter placeholders.
   - ECS service/task and ALB rule are skipped unless `worker_image`,
     `existing_vpc_id`, `existing_private_subnet_ids`,
     `existing_alb_listener_arn` are set — fine to skip for this step.
3. Run the rehearsal against real infra: point `CLHEAR_EVENTS_QUEUE_URL` at the
   new queue, run `run_dummy_fleet` + `relay_once` with `SqsTransport`, consume
   with `handle_envelope`, approve the proposal via the API, export a snapshot.
   (Mirror `tests/test_dummy_fleet_rehearsal.py` steps 1–5 with SQLite locally
   or Aurora if a DSN is available in `/clhear/DATABASE_URL`.)
4. Set the real SSM parameter values (`/clhear/ANTHROPIC_API_KEY` optional,
   `/clhear/DATABASE_URL` when Aurora is wired).
5. Comment results (apply outputs + rehearsal evidence) on PR #1 and mark it
   ready for review.

## Then: P1–P4 (one PR per phase, HLD §9)

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

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

## P1 — DONE (2026-08-21), branch `cursor/clhear-p1-l1-verbatim-9acd`

- `l1_sources` migration (m0002) + models; adapter contract
  (`app/clhear/l1/adapters/base.py`); `pipeline.py` (hash → S3 originals →
  version+clauses upsert → clause diff by ref → `change_events` +
  `SourceChanged` in one transaction); `families.py` citator sync.
- **Adapters shipped ahead of plan** (P2 breadth pulled forward for the live
  UI): `uk_legislation` (CLML + effects-feed citator), `eur_lex` (Cellar
  consolidated XHTML + corrigenda probe), `govinfo_us` (USC HTML + eCFR API,
  FATCA statute+regs), `nist` (OSCAL 800-53 r5.2, CSF 2.0 CPRT export).
  All run green OFFLINE against recorded fixtures (`tests/fixtures/http`,
  `CLHEAR_HTTP_MODE=replay|record|live` in `l1/http.py`).
- **Done-test passed** (`tests/test_l1_pipeline.py`): MLRs fully ingested
  (159 clauses); historical replay (point-in-time 2020-01-09 → current) yields
  correct clause-diff (`regulation-3` et al amended) + `SourceChanged`; family
  auto-contains the 38 amending instruments incl. `uksi/2019/1511` via citator.
  Plus restricted-discipline tests (text never leaves for `public_ok=false`).
- **Live UI deployed**: https://wpje8c1y3a.execute-api.us-east-1.amazonaws.com
  (`/sources` Explorer — library, verbatim clause browsing, search, change
  events, version pills). Corpus: 4 families, 6 ingested sources, 1938 public
  clauses; originals in `s3://reg42-clhear-datalake/public-ok/…` (Object Lock).
  Stack: Lambda (`app/clhear/lambda_web.py`, Mangum) + API Gateway HTTP API
  (`infra/webui.tf`) — a plain Function URL is SCP-blocked in this account.
  # ARCH: swaps to the reg42-os web service + ALB host rule when wired.
  Rebuild/redeploy: `scripts/build_corpus.py` then `scripts/deploy_webui.sh`.
- Not in P1: embeddings/semantic search (P2), citation mining + reconciliation
  (P2), E1–E7 evals (P4), restricted importers + BYOL (P3). Search is LIKE-based
  for now (pg_trgm/BGE-M3 when Aurora + P2 land).

## Next: P2–P4 (one PR per phase, HLD §9)

- **P2** — `families.py` citation mining + reconciliation; `embeddings.py`
  (BGE-M3, CPU). Adapters for eur_lex/govinfo/NIST already landed in P1.
  Done-test: E2 100% on GDPR; semantic search returns MLR reg 27–28 for
  "customer due diligence"; citations table populated with 0 unexplained on MLRs.
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

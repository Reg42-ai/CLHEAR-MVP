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

- `l1_sources` migration (m0002) + models including **`doc_nodes`** (typed raw
  document tree: type/ref/label/heading/raw_text/source_fragment/parent/seq)
  and `clauses` as the provision-level projection (`doc_node_id` FK);
  adapter contract is now `DocNode` (`app/clhear/l1/adapters/base.py`);
  `pipeline.py` persists the tree then derives clauses (subtree-text
  concatenation) → clause diff by ref → `change_events` + `SourceChanged`;
  `families.py` citator sync.
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
  (`/sources` Explorer reconstructs the original document from `doc_nodes` —
  serif layout, hover badge with ref/hash/amended, click-pinned inspector
  with record id / sha256 / version / S3 original / change history /
  permalink; document/inspect toggle). `GET /api/clhear/sources/{key}/document`
  and `GET /api/clhear/nodes/{id}`. Search and change-event refs deep-link
  to `#node-<id>`.
  Stack: Lambda (`app/clhear/lambda_web.py`, Mangum) + API Gateway HTTP API
  (`infra/webui.tf`) — a plain Function URL is SCP-blocked in this account.
  # ARCH: swaps to the reg42-os web service + ALB host rule when wired.
  Rebuild/redeploy: `scripts/build_corpus.py` then `scripts/deploy_webui.sh`.
- Mini-E3 round-trip test: concatenated public `raw_text` of the MLR document
  matches the official CLML Body+Schedules Text nodes (whitespace-normalized
  length within 15%; distinctive CDD span present). Restricted discipline
  covers `raw_text` and `source_fragment` on both `/document` and `/nodes/{id}`.

## Fidelity gates + repair loop + audit trail — DONE (2026-08-23)

Root-cause fix after the GDPR fidelity gap: adapters are now MEASURED against
their artifacts on every ingest, and the fleet learns from failures.

- **Fidelity gate** (`app/clhear/l1/fidelity.py`): every adapter implements a
  deliberately-dumb `expected_text()` oracle (all visible artifact text minus
  declared exclusions); the pipeline requires token coverage >=
  `CLHEAR_FIDELITY_THRESHOLD` (0.995) AND zero contract-invariant violations
  (label never duplicated in text, unique refs, clause-grain nodes have refs)
  before ANYTHING persists. All 6 adapters + the GDPR OJ original are at
  100.00% coverage.
- **Repair loop** (`pipeline.ingest`): parse → learned `parse_hints` (tier 1b,
  zero LLM) → LLM-proposed hints via the L0 gateway (fleet `l1.repair`,
  spend-capped, structured output; the LLM only CLASSIFIES artifact spans —
  it never writes text) → bounded salvage (`CLHEAR_SALVAGE_CAP` 2%) →
  re-fetch; up to `CLHEAR_INGEST_MAX_ATTEMPTS`. On exhaustion: nothing
  persisted, "ingest NOT fully successful" logged, `IngestFidelityFailed`
  event + `ingest_rectification` proposal (pending manual rectification in
  /review). Gate-passing LLM hints persist to `parse_hints` and apply
  deterministically on all future runs; ratify/retire via the proposal
  (approval hook in `platform/proposals.py`).
- **`l1_fidelity` eval suite** registered in the P0 harness — runs per source
  offline in CI and blocks releases via the existing gate (E2/E3 skeleton).
- **Document fidelity**: GDPR now has TWO versions — the ORIGINAL OJ act
  (`oj-32016R0679`: title block, preamble citations, 173 recitals, points,
  signatures, footnotes) and the consolidated text; the corrigendum lands as
  a real change event (11 articles amended). UK prelims (banner, dates,
  enacting text) ingested. USC parses only the statute field (deep heads
  included); eCFR captures outline/example/CITA elements.
- **Activity + Fleet UI** (`/sources` tabs): Activity = day-grouped audit
  timeline over runs/change_events/events/eval_runs (status dots, fleet
  badges, version-update entries, /review links; metadata only — never
  clause text). Fleet = per-source health board (version, coverage, stage
  chips, freshness) + per-run SVG pipeline DAG with stage timings and replay
  animation; polls while a run is `running`. APIs: `/api/clhear/activity`,
  `/fleet`, `/runs/{id}`. Run rows are written at START with appended stage
  transitions (fetch/parse/gate/hints/llm_repair/salvage/persist/diff).
- 30 tests green, incl. fleet-wide parametrized gate tests, loop convergence/
  exhaustion, hint memory (zero repeat LLM calls), retirement, activity feed.
- Not in P1: embeddings/semantic search (P2), citation mining + reconciliation
  (P2), E1–E7 evals (P4), restricted importers + BYOL (P3). Search is LIKE-based
  for now (pg_trgm/BGE-M3 when Aurora + P2 land).

## Standardized version model + provenance + job canvas — DONE (2026-08-23)

- **Version model**: `source_versions.version_kind`
  (`as_published|consolidated|edition`) + `as_of_date`; standardized labels
  `{kind}:{as_of|id}`. Currency is a STATUS (`in_force`/`superseded`), kind is
  a DESCRIPTOR — never conflated. Per-adapter `version_policy` declared in
  SourceMeta. Two-tier ingestion policy: as-published baseline once where the
  publisher provides one (UK as-made 2017, GDPR OJ), current text tracked
  daily. MLR now has 3 text states; GDPR 2. `VERSION_KINDS` dictionary (plain-
  language definitions) served via `GET /api/clhear/meta` and rendered as
  tooltips + a "version kinds ⓘ" legend in the UI.
- **User-facing model**: one "Current text" badge per source; a Provenance
  panel unifying text states (oldest-first) + the family instruments that
  caused the changes (`/sources/{key}` `provenance` block). The as-published
  preamble notice links consolidated views to the original act. Informative-
  tier drafts/consultations slot into the same panel in P2+ (watchers).
- **Curated context**: `sources.about` + `topics` authored in SourceMeta
  (deterministic, code-reviewed; zero LLM). Generated semantics stay OUT of
  L1 — they get an annotations table with provenance in L2.
- **Fleet job canvas**: every fleet execution carries a `job_id` (RunRecorder
  inputs; relay recorded as its own `l0.relay` run). `GET
  /api/clhear/jobs/latest` derives the task graph from the runs ledger; the
  Fleet tab leads with a Databricks-style SVG workflow (lanes per source,
  status-colored task cards, edges converging on relay, live polling while
  running, job replay), with the per-run stage DAG as click-through and the
  table behind a toggle.
- 41 tests green; corpus rebuilt (25,116 nodes, 9 versions across 6 sources);
  deployed + browser-verified end to end.

## Reader efficiency + clause understanding layer — DONE (2026-08-23)

- **Pipeline education**: `STAGE_INFO` dictionary (models.py) explains every
  stage in plain language; served via `/api/clhear/meta`; tooltips on stage
  chips/DAG nodes + "stages ⓘ" legend on the Fleet tab; job header shows
  nodes-processed throughput (timings are real — the corpus is just fast).
- **Short names**: `sources.short_name` ("GDPR", "UK AML Regulations
  (MLRs 2017)", "FATCA statute", …) used across library, document headers,
  Fleet canvas/table, activity and search; official titles stay as subtitles.
- **Reader tools**: TOC sidebar (client-side from nodes), in-document filter
  (text + annotation-category chips), grouped search results with category
  filters (`/api/clhear/search?category=&topic=`).
- **Clause understanding layer** (`clause_annotations` — enrichment ABOUT the
  verbatim text, never the text): Tier 1 heuristic classifier runs in-pipeline
  (`annotate` stage; categories definitions/obligation/prohibition/scope/
  enforcement/procedure/exemption/administrative + topics inherited from
  curated source metadata) — 2,112 clauses classified in the live corpus.
  Tier 2 LLM explainer (`scripts/annotate_corpus.py`, gateway fleet
  `l1.annotate`, ~$2 for the corpus, FakeProvider-tested, idempotent) is
  built and WAITING ON `ANTHROPIC_API_KEY` — add it as a Cloud Agent secret
  or SSM `/clhear/ANTHROPIC_API_KEY`, then run:
  `DATABASE_URL=sqlite:///deploy/clhear.db python scripts/annotate_corpus.py`
  and redeploy. UI marks AI output "AI-generated explainer — not legal text".
- 51 tests green; corpus rebuilt + deployed + browser-verified (6/6).

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
- Any path that could emit clause text, `raw_text`, or a `source_fragment`
  goes through `app.clhear.l1.public` (`clauses_public` / `nodes_public`) or
  a BYOL check — write the tests.
- Adapters must run green offline against recorded fixtures; never hammer
  official endpoints.
- Anything ambiguous: smaller change + `# ARCH:` comment. Never invent scope.

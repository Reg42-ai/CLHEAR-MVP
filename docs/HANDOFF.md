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
- **Live UI deployed**: https://clhear.reg42.ai
  (execute-api fallback: https://wpje8c1y3a.execute-api.us-east-1.amazonaws.com)
  (`/sources` Explorer reconstructs the original document from `doc_nodes` —
  serif layout, hover badge with ref/hash/amended, click-pinned inspector
  with record id / sha256 / version / S3 original / change history /
  permalink; document/inspect toggle). `GET /api/clhear/sources/{key}/document`
  and `GET /api/clhear/nodes/{id}`. Search and change-event refs deep-link
  to `#node-<id>`.
  Stack: Lambda (`app/clhear/lambda_web.py`, Mangum) + API Gateway HTTP API
  (`infra/webui.tf`) — a plain Function URL is SCP-blocked in this account.
  # ARCH: swaps to the reg42-os web service + ALB host rule when wired.
  Rebuild/redeploy: `scripts/build_corpus.py` then `scripts/deploy_webui.sh`
  (Lambda deps come from `requirements.txt`, resolved for manylinux/cp312).
  Worker image without local Docker: `scripts/build_worker_image.sh` zips the
  build context to the deploy bucket and runs the CodeBuild project from
  `infra/codebuild.tf`, pushing `clhear-workers:latest` + a timestamp tag.
  GitHub Actions assume `clhear-github-release` via OIDC (`infra/github_actions.tf`).
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

## Hybrid knowledge retrieval (Cerebras lessons) — DONE (2026-08-24)

Applied from the Cerebras Knowledge write-up (x.com/cerebras article):
- **`search_units`** (models.py): unified store — every PUBLIC clause in
  DISTILLED form (short name + path + classification + text, LLM summary
  folded in when the explainer runs) + paragraph-grain "burst" units
  (clause heading prepended, ≥120 chars). Built by the pipeline `index`
  stage; only in-force versions; restricted sources never indexed.
  FTS5 mirror for BM25 (verified working on the Lambda runtime; falls back
  to LIKE if absent).
- **`l1/retrieval.py`**: citation router (reg 27 / art 6 / §1471 /
  1.1471-5(b) / ac-2 / GV.OC-01) + FTS5 + LIKE retrievers fused with
  Reciprocal Rank Fusion (weight/(60+rank)), per-clause dedup, per-source
  cap, context restoration (clause path / sibling preview). The P2
  embedding retriever plugs into the same fusion (add a ranked list).
- `/api/clhear/search` + UI: scope chips (family "projects"), grouped
  results with context lines.
- Reader filter FIXED (subtree-aware visibility: category inherited from
  clause ancestors, text matched on descendants, matches+ancestors+
  descendants shown); taxonomy simplified to 4 types (definition/
  requirement/enforcement/other — plural-aware "Definitions" regex);
  compact reader bar above the document; inspector "contains" preview;
  zero-match empty-state message.
- 56 tests green; corpus rebuilt (5,969 search units) + deployed; live
  checks: "reg 27"→regulation-27 first, "legitimate interests"→art_6.1(f)
  paragraph burst first with context, scope=uk-mlr filters correctly.

## eToro design-partner scope (L1 blueprint) — DONE (2026-08-24)

- Input: `docs/etoro-clhear-source-registry.md` (draft v0.1 — ~200 registry
  IDs across 15 legal entities, source classes SC1–SC5, clusters A–F).
- `docs/ETORO_L1_SCOPE.md`: full registry-ID → CLHEAR mapping — 20 families,
  per-source adapter assignment, feasibility classes (A param-reuse /
  B new-HTML-adapter / C Docling-PDF / D restricted / E list-feeds /
  W watchers), 4 import waves. Wave 1 (~50 sources) needs ZERO new adapters:
  eur_lex takes any CELEX id, uk_legislation any legislation.gov.uk doc,
  govinfo title 26 already wired.
- `scripts/seed_registry.py`: idempotent seed — 20 families + 96
  reference-level sources (`added_via='watchlist'`) with short_name/about/
  topics/registry-IDs/wave; attaches already-known keys (DPA 2018, PSRs 2017)
  to blueprint families instead of duplicating; restricted standards (ISO,
  SOC 2 TSC, PCI, IFRS) flagged `license='restricted'` for the P3 importer.
- Library UI: charter registry notes, "planned" state for watchlist members,
  footer "6 ingested · 96 planned (eToro blueprint)". Deployed + verified live.
- Blockers needing eToro input before scope freeze are listed in the scope
  doc (§20 verify items: eToroX Gibraltar status gates F16-GI, QI entity map,
  clearing/CAT split vs Apex, ISA manager of record, SFTR lending, …).
- Next concrete step: Wave-1 ingestion — run the existing pipeline over the
  seeded CELEX/UK keys (fixtures + fidelity gates per doc; expect per-doc
  parse-hint work for directives with different CONVEX quirks).

## Wave-1 eToro ingest + daily fleet schedule — DONE (2026-08-25)

Honest status of the daily fleet *before* this work: EventBridge rules
existed (`rate(1 day)` for UK/EUR-Lex) but were **DISABLED**, the
`clhear-cluster` had **no worker service**, and the last corpus had 6
ingested sources + 96 planned. The fleet did **not** run daily.

Now:
- **Wave-1 ingested live** through the fidelity gate: 53/53 class-A sources
  (EU CELEX + UK legislation.gov.uk) plus the original starter (GDPR, MLRs,
  FATCA, NIST) = **59 ingested versions**. Parser generalizations: OJ annex
  flow, `tis_*` titles, CONVEX `art_14a` suffixes, UK `EURetained` +
  `EUPreamble` recitals, unique-ref dedup. `uksi/1986/1711` stays planned
  (image-only, 504 from legislation.gov.uk).
- **Schedule is visible in the UI** (`FLEET_SCHEDULES` via `/api/clhear/meta`):
  UK daily 05:20 UTC, EUR-Lex daily 05:40 UTC, GovInfo/NIST weekly Mon 06:00.
- **Workers handle `AdapterRunRequested`**: EventBridge cron → SQS →
  `run_adapter_fleet` over the Wave-1 plan; snapshot SQLite published to
  `s3://…/webui/clhear-latest.db`; Lambda re-fetches on a 5-min TTL.
- Enabling the cron + deploying the worker image is the remaining infra
  apply (ECR + ECS service on the default VPC, `schedules_enabled=true`).

## Go-live state — 13 Sep 2026 (branch `cursor/clhear-hld-v2-325d`, PR #10)

What is live in account 730649732189 / us-east-1 after the go-live push:

- **Record on Aurora Serverless v2** (`clhear-record`, PG 16.13, pgvector 0.8.1,
  0–8 ACU, auto-pause 30 min, 35-day backups, deletion protection). Loaded from
  the v1 snapshot with `app.clhear.tools.load_record` (90 tables, 267,512 rows,
  migration ledger identical, no dangling why-trails). Fleets, the web tier and
  `clhear-infer` all read and write it; `record_cutover = true` in
  `infra/terraform.auto.tfvars` is the switch, `/clhear/DATABASE_URL` the DSN.
  The S3 snapshot `webui/clhear-latest.db` is no longer written; releases ship
  `l1/snapshot.db` exported from Aurora by `record_copy.export_sqlite_snapshot`.
- **Network**: two private subnets (`172.31.96.0/20`, `172.31.112.0/20`) with
  their own route table through NAT `clhear-nat` (`infra/network.tf`). The web
  Lambda runs in them (`vpc_config`), so console decisions, votes and API keys
  now persist (verified: a maintainer write through `clhear.reg42.ai` is in
  Aurora on a separate connection). The shared public subnets are untouched.
- **Inference**: `clhear-infer` (`infra/infer.tf`), CLHEAR's private Reg42 Infer
  router on Cloud Map `infer.clhear.local:8000`, image pinned by digest with the
  catalog in `infra/infer-catalog/` (rendered from `task_classes.py`; only
  procurement-clean Bedrock models exist in it, keyed by Bedrock id). Principal
  `clhear`, USD 250/day, USD 2,000/month hard stop, spend ledger in the `infer`
  database on Aurora. Verified from a fleet task: every task class lands on its
  clean champion; unknown classes land on the derivation tier. Operating notes
  and the migration recipe to `kernel/infer`: `handoff/reg42-infra/README.md`.
- **Images**: `clhear-workers:latest` (entrypoint `python`, default command the
  worker, `postgresql-client` for the DR drill) and `clhear-infer:latest`, both
  built by CodeBuild via `scripts/build_worker_image.sh [workers|infer]`.
  One-off jobs are ECS `run-task` command overrides on `clhear-fleet-l0`
  (record load, nightly stack, DB peeks) — no Docker needed anywhere.
- **Models that answer in this account**: `openai.gpt-oss-120b-1:0`,
  `mistral.mistral-large-3-675b-instruct`, `us.anthropic.claude-opus-4-5-20251101-v1:0`,
  `amazon.nova-lite-v1:0`, `amazon.nova-pro-v1:0`, `us.meta.llama3-3-70b-instruct-v1:0`,
  `amazon.titan-embed-text-v2:0`. Claude Sonnet/Opus 5 are not available to
  the account (AWS sales) and no other Anthropic model answers until the
  Anthropic use-case form is accepted in the Bedrock console.
- **Gateway lessons from the first real derivation**: reasoning models spend
  `max_tokens` on hidden thinking, so Infer requests carry +2048 headroom; the
  JSON parser takes the first complete object because gpt-oss keeps talking
  after it, and when the model enumerates several objects `l2.structured`
  keeps the one closest to the statement. All in `platform/gateway.py` with tests.
- **Embeddings**: `CLHEAR_EMBEDDING_PROVIDER=bedrock` on fleets and web —
  `BedrockEmbedder` calls Titan Text Embeddings v2 directly (with backoff; the
  account quota is roughly 5 requests/s) because the pinned Infer image has no
  `/embeddings` route. IAM allows only the two embedding model ARNs; the
  never-list test names the exemption. Flip to `infer` when the route lands.
- **First derivation** (13 Sep, one L0 task, 2 vCPU / 4 GB, 4 h 04 min, USD 7.64
  across 4,752 Infer calls — Mistral Large 3 for `l8.fill` was USD 6.87 of it;
  everything else ran on gpt-oss-120b and Nova Lite for cents). Stage output:
  3,440 obligations (3,418 decomposed into 2,475 derived blocks + 10 curated),
  6,120 characteristics backed, 4,134 applicability edges, 3,287 obligations
  mapped to 2,039 activities, 2 blueprints composed, 4,715 fills drafted,
  graph projection 20,026 nodes / 33,302 edges, 7,751 clauses embedded on
  Titan v2 (pgvector). Bugs it surfaced, all fixed and covered by tests:
  SQLite-only FTS probe poisoned the Postgres transaction (hybrid search was
  returning 500 on Aurora), `l8.fill` required-key derivation crashed on two
  shapes, Infer has no `/embeddings` route (Titan direct, see trace), pre-m0009
  clauses carried `normative = false` (backfill now runs first in the stack).
- **Release `2026.09.13`**: `s3://clhear-deploy-730649732189/releases/2026.09.13/`
  (`manifest.json`, `l1/snapshot.db` 512.8 MiB exported from Aurora, sha256
  `ee301eff…79c25`, `.reserved` markers for L2–L8, frozen model manifest).
  Ships **L0 + L1**; everything above is reserved until the gates below clear.
  The 23:30 UTC `eod-publish` schedule re-cuts the day's release from the L0
  fleet; the exporter was rehearsed locally with the disclosure flag off (176
  files, `pushed: false`).
- **Gate status** (30 of 37 suites green; per layer: L0, L1, L4, L6, L8 pass
  their own suites — L4/L6/L8 stay reserved because L2/L3/L5 below them are
  blocked):

  | Suite | Score | Threshold | Why it fails | Who clears it |
  |---|---|---|---|---|
  | `l2_precision` | 0.55 on 47 model-judged, 0 expert votes | 0.95 | needs maintainer votes | you (console) |
  | `l3_precision` | 0 votes | 0.92 | needs maintainer votes | you (console) |
  | `l5_precision` | 0 votes | 0.92 | needs maintainer votes | you (console) |
  | `l2_coverage` | 0.88 (3,437 / 3,902 normative clauses asserted) | 0.99 | `l2/extract.py` modality patterns are narrower than `l1/spans.is_normative` (prohibitions, "ensure that", offences, liability) | code: widen `MODALITY_PATTERNS`, bump `EXTRACTOR_VERSION`, re-derive |
  | `l3_l5_referential` | 6 extraction misses | 0 | curated blocks/activities cite GDPR art. 6 and 32, which the extractor does not yield as obligations | same extractor change |
  | `l5_completeness` | 29 business-side orphans | 0 | activities like `ACT-AI-mtf-otf-trading-process` have no product/service that implies them | curated L4 ontology (`implies` edges) |
  | `l7_brier` | no published calibration run | beats baseline | too little enforcement-outcome history yet | accrues with the enforcement adapters |
- **DR drill on Aurora** (HLD item 17): the 04:00 UTC `DrDrillRequested` event
  now passes all three legs from the L0 fleet — `pg_dump` 17 → `pg_restore` into
  `clhear_drill` on the same cluster (55 tables, 368k rows, migration ledger
  equal, no dangling why-trails), graph projection checksum equal, and the
  datalake replica sampled. RPO/RTO measured at 39 s against 24 h / 4 h targets.
  What the first runs surfaced, fixed in `platform/dr.py` with tests: the DSN
  (with password) rode in `pg_restore` argv and in the recorded failure — the
  password now travels in `PGPASSWORD`; `pg_dump` 17 emits `SET transaction_timeout`
  that PostgreSQL 16 rejects, making a complete restore exit 1 — that exact
  skew (session `SET` of an unknown parameter) is tolerated, nothing else; and
  the verification compared the restore with a live count taken minutes later,
  so fleets writing during the drill read as a failed restore — each restored
  count must now lie inside the bracket of source counts taken just before and
  just after the dump, and the restored graph may match the pre-dump live
  projection. `dr_drilled` on `/status.json` is met.
- **Datalake replica**: `replication_enabled = true` — `reg42-clhear-datalake-replica`
  in eu-west-1 (Object Lock, versioning, STANDARD_IA, 15-min RTC). The 540
  objects (794 MB) that predate the rule were backfilled once with S3 Batch
  Replication (job `670df634…`, 2,508 versions, 0 failed; role
  `clhear-datalake-batch-replication`, created by hand, can be deleted).
- **Status page**: `/status.json` names the release `latest.json` points at
  (`2026.09.13`); it was `null` because the web role could `GetObject` but not
  list `releases/`, and `_release` swallowed the AccessDenied. The role may
  now list that prefix (`/v1/releases` works) and the status page reads the
  pointer directly. Overall status stays `degraded` on `gates_green` alone
  until the table below clears — `api_availability`, `freshness_tier_a`,
  `derived_freshness` and `dr_drilled` are met.
- **SES**: identities and DKIM verified, but production access was DENIED
  (case 178406808100114) — sandbox, verified recipients only. Cognito sign-in
  uses `COGNITO_DEFAULT` mail and is unaffected.
- **Cognito**: pool `us-east-1_gVVpIhrgl`, hosted UI `clhear-auth` ACTIVE,
  identity providers: Cognito only (Google IdP needs the client secret).

### Runbook

- Redeploy web: `scripts/deploy_webui.sh` packages `deploy/webui-<stamp>.zip`; upload it
  to `s3://clhear-deploy-730649732189/webui/`, set `webui_zip_key`/`webui_zip_sha256`
  in `terraform.auto.tfvars`, `terraform apply`. (The script's `deploy/clhear.db`
  upload is snapshot-mode only; do not overwrite the S3 snapshot in Aurora mode.)
- Rebuild fleets: `scripts/build_worker_image.sh` then `terraform apply` (task
  definitions reference `:latest`; running services pick it up on scale-out).
- Change routing/caps: edit `task_classes.py` or the render arguments, run
  `scripts/render_infer_catalog.py`, `scripts/build_worker_image.sh infer`,
  `terraform apply`.
- One-off job: `aws ecs run-task --cluster clhear-cluster --task-definition clhear-fleet-l0
  --launch-type FARGATE --network-configuration '...' --overrides '{"containerOverrides":
  [{"name":"worker","command":["-m","app.clhear.fleets","nightly","--release","<id>","--force"]}]}'`.
- Run the DR drill now: send the EventBridge envelope by hand —
  `aws sqs send-message --queue-url https://sqs.us-east-1.amazonaws.com/730649732189/clhear-fleet-l0
  --message-body '{"event_id":"manual-dr-drill-<date>","layer":"l0","kind":"DrDrillRequested",
  "subject_ref":"all","payload":{"neo4j_database":"drill"},"schema_version":1,"producer":"operator","ts":""}'`;
  the result lands in `l0_platform.dr_drills` and on `/status.json` `dr` within ~5 min.
- Rollback to snapshot mode: `record_cutover = false`, apply (fleets and web
  return to `webui/clhear-latest.db`; Aurora keeps everything written since).
- Neo4j stays off (`neo4j_enabled = false`): `LocalGraph` rebuilt the full
  projection in under a second; `hq/brain` is the intended home later.

### Tasks only a human can do (in order)

1. **Votes** — sign in at `https://clhear.reg42.ai/console` with a
   `CLHEAR_MAINTAINERS` address and judge the precision samples: L2 (47 model
   judgements waiting for expert confirmation; ≥ 0.95 agreement needed), L3
   and L5 (no votes yet; ≥ 0.92). Layers unlock in the next release once each
   threshold is met.
2. **Public repo** — create `Reg42-ai/clhear` (org owner) and a fine-grained PAT
   with `contents: write` on that repo only.
3. **GitHub Actions secrets** on `CLHEAR-MVP` (Settings > Secrets and variables >
   Actions): `AWS_RELEASE_ROLE_ARN` = `arn:aws:iam::730649732189:role/clhear-github-release`
   (switches `release.yml` into *fleet* mode: fetch the L0-cut release, sign,
   SBOM, verify, upload only those files, export from the released snapshot),
   `CLHEAR_PUBLIC_REPO_URL`, `CLHEAR_EXPORT_GIT_TOKEN`. `INFER_*` secrets are
   not needed: the router is VPC-only and CI never derives against it. Leave
   the variable `CLHEAR_PUBLIC_DISCLOSURE_CONFIRMED` unset until you have read
   `releases/2026.09.13/manifest.json` `reserved_layers`.
4. **Bedrock** — accept the Anthropic use-case form in the Bedrock console (enables
   the Claude rungs); ask the AWS account team for Claude Sonnet/Opus 5.
5. **SES** — reopen case 178406808100114 for production access (contributor
   magic links to unverified addresses).
6. **Google sign-in** — OAuth client with redirect
   `https://clhear-auth.auth.us-east-1.amazoncognito.com/oauth2/idpresponse`;
   put id/secret in SSM `/clhear/GOOGLE_CLIENT_ID` / `/clhear/GOOGLE_CLIENT_SECRET`,
   then `terraform apply`.
7. **Publish** — set `CLHEAR_PUBLIC_DISCLOSURE_CONFIRMED=true` and run the
   `release` workflow. Optional later: Discourse, beehiiv, Upptime, pen-test
   vendor, SAML IdP metadata.

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

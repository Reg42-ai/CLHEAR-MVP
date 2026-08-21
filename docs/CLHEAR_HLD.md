# CLHEAR — High-Level Design (HLD)
### Chief Architect's finalized build document · v1.0 · August 2026

**Owner:** Avner Yoffe, Reg42 · **Builder:** Cursor, in `reg42-os` and `reg42-infra`
**Build scope of this HLD:** Layer 0 (platform plane) + Layer 1 (verbatim source layer), production-grade, on the existing AWS environment. Layers 2–8 are context here and get their own HLDs later.
**This document supersedes and consolidates** `CLHEAR_SYSTEM_ARCHITECTURE.md` and `CLHEAR_L1_BUILD_MANUAL.md` for build purposes. When in doubt, this file wins.
**Diagrams:** `clhear-l1-architecture.drawio` / `.png` (master visual for L1).

---

## 1. What we are building, in plain language

CLHEAR is a live, always-on system — not a script that runs once. What we build now is its foundation:

1. **A vault of regulatory truth (L1).** The system automatically fetches the official text of laws, regulations, and standards from their authoritative sources, breaks each document into clauses, stores the originals immutably, and watches for changes forever. When a regulation changes, the system detects it at clause level and announces it.
2. **The completeness machinery.** A "source" is never one document — MLRs 2017 is the base law plus every amendment; FATCA is a statute plus regulations plus IRS guidance plus intergovernmental agreements. The system tracks the whole *family* of each source using official amendment registries, mines citations out of the texts, and proves that nothing is missing.
3. **Public evidence (evals).** For every source, the system publishes a scorecard proving the stored corpus is byte-accurate, complete, and current — reproducible by anyone. This is what lets the world trust CLHEAR.
4. **The rails everything else runs on (L0).** One event bus, one AI gateway with cost control, one human-approval console, one release pipeline. Every future layer (obligations, building blocks, profiles, the composer) reuses these rails.

Nothing user-facing about compliance programs exists yet at the end of this build — what exists is a trustworthy, self-updating, provably-complete regulatory corpus with a public browsing UI. That is deliberately the foundation: every later layer stands on it.

---

## 2. System context (the eight layers — orientation only)

```
 L8 Benchmarks (closed) · L7 Risk scoring · L6 Program Composer + Explorer
 L5 Activities (business+compliance junction) · L4 Profile space
 L3 Building blocks · L2 CLHEAR obligation registry + change inference
 ──────────────────────────────────────────────────────────────────────
 L1 VERBATIM SOURCES  ◄── this build          L0 PLATFORM ◄── this build
```

CLHEAR is **one instance, no customers inside it** — global schemas, no tenancy. Reg42 OS (separate, existing, RLS multi-tenant) will later consume CLHEAR releases read-only.

---

## 3. Non-negotiable principles

1. **Verbatim ground truth.** Original artifacts stored immutably (S3 Object Lock/WORM); clause text in Postgres always traceable to a hashed original. Never re-generate or "clean up" source text.
2. **Determinism first, LLM last.** L1 is deterministic: fetch, parse, hash, diff, reconcile. LLMs appear only in narrow, review-gated triage (watcher candidate classification). If a step can be code, it is code.
3. **Named-human gates.** No family membership, no candidate acceptance, no release without a recorded human approval (approver identity + timestamp). Agents propose; humans ratify.
4. **Restricted zone discipline.** ISO 27001 / SOC 2 TSC text lives only in `restricted/` storage and `public_ok=false` rows. It never reaches the public repo, public UI, or any external API. Enforced by IAM, by DB views, and by the similarity guard in CI.
5. **Evals are gates, not reports.** A release that fails E1–E7 thresholds does not ship. Scores publish with every release.
6. **Cost posture: near-zero idle.** Reuse the existing Aurora cluster and ALB; min-0 Spot workers; CPU embeddings; **no GPU anywhere in this build**. Target ≈$5–10/month marginal AWS spend.
7. **Everything is replayable.** Every fleet run recorded (inputs, outputs, versions, cost). Same inputs ⇒ same corpus.

---

## 4. Repositories & environments

- **`reg42-os`** — new package `app/clhear/` (code below) + Explorer frontend page(s) in the existing single-file React 18/htm no-build pattern, served by the existing web service at hostname `clhear.reg42.ai`. Python 3.10, FastAPI, startup migrations — follow existing repo conventions exactly (migration numbering, settings, auth deps). Feature flag: `REG42_CLHEAR_ENABLED`.
- **`reg42-infra`** — Terraform additions (§5). us-east-1, existing account.
- **`clhear`** — NEW public GitHub repo (create empty now; the exporter fills it): `/spec`, `/evals` (public golden sets + published scores), `/runners` (reproducible eval code for open sources), `/snapshots` (per release). License: Apache-2.0 for code, CC BY 4.0 for spec text. **Never any restricted text, ever.**

Package layout in `reg42-os`:

```
app/clhear/
  platform/            # L0
    events.py          #   outbox writer + SQS relay + envelope schema
    gateway.py         #   LLM provider abstraction, spend caps, call ledger
    proposals.py       #   unified l0_proposals model + approve/reject api
    evals.py           #   harness runner + l0_eval_runs + JSON export
    exporter.py        #   release snapshot -> public repo (git push via token)
  l1/
    adapters/          #   base.py (contract) + uk_legislation.py, eur_lex.py,
                       #   govinfo_us.py, irs_gov.py, restricted_file.py, watchers.py
    pipeline.py        #   hash/version/diff/upsert orchestration
    families.py        #   citator sync + citation mining + reconciliation
    embeddings.py      #   BGE-M3 batch job (sentence-transformers, CPU)
    guard.py           #   similarity guard vs restricted corpus
    evals/             #   e1_fidelity.py … e7_closure.py + golden/
    routes.py          #   /api/clhear/sources… + BYOL endpoints
  workers.py           # SQS consumer entrypoint (ECS clhear-workers)
```

---

## 5. AWS infrastructure (Terraform inventory — all additive, nothing replaced)

| Resource | Spec | Notes |
|---|---|---|
| S3 `reg42-clhear-datalake` | Versioning ON; **Object Lock ON at creation (cannot retrofit)**, compliance mode, sensible default retention; prefixes `public-ok/`, `restricted/`, `byol/{user}/`; lifecycle → IA @30d | Bucket policy: no public access; `restricted/` readable only by worker task role |
| SQS `clhear-events` (+DLQ) | Standard queue; DLQ alarm on depth>0 | The single event plane |
| EventBridge rules | One per adapter schedule: uk_legislation daily, eur_lex daily, govinfo weekly, irs_gov weekly, catalog watchers weekly | Each triggers an ECS run-task or enqueues a job message |
| ECS service `clhear-workers` | Fargate **Spot**, same image as reg42-os, entrypoint `python -m app.clhear.workers`, min 0 / max 2, scale on SQS depth | No new image, no new pipeline |
| Aurora (existing cluster) | New schemas `l0_platform`, `l1_sources`; `CREATE EXTENSION IF NOT EXISTS vector`; roles `clhear_writer` (workers), `clhear_reader` (web app, sees `clauses_public` view only) | Global schemas — deliberately **no tenant_id, no RLS** |
| ALB + Route53 + ACM | Host rule `clhear.reg42.ai` → existing web service | Public UI |
| SSM Parameter Store | `ANTHROPIC_API_KEY` (gateway, optional use), GitHub deploy token for exporter | Never in env files |
| CloudWatch | Dashboard: crawl freshness per source, queue depth, DLQ, daily LLM spend, eval trend; alarms: DLQ>0, spend>cap, freshness SLA breach | |

Spend caps in gateway config: **$20/day per fleet, $100/day global, hard stop + alarm.**

---

## 6. Data architecture

### 6.1 `l0_platform`

```sql
events(id bigserial pk, event_id uuid unique, layer text, kind text, subject_ref text,
       payload jsonb, schema_version int, producer text, created_at timestamptz,
       relayed_at timestamptz)                       -- transactional OUTBOX; relay ships to SQS
proposals(id uuid pk, layer text, kind text, subject_ref text, draft jsonb, rationale text,
       confidence numeric, status text check (proposed|approved|rejected),
       approver text, decided_at timestamptz, created_at timestamptz)   -- ONE table, all layers
llm_calls(id bigserial, fleet text, provider text, model text, prompt_hash text,
       input_tokens int, output_tokens int, cost_usd numeric, created_at timestamptz)
eval_runs(id bigserial, suite text, source_key text, release text, scores jsonb,
       passed bool, ran_at timestamptz)
runs(id bigserial, fleet text, trigger text, inputs jsonb, outputs jsonb,
       duration_ms int, created_at timestamptz)      -- run ledger
```

Envelope (frozen — most expensive thing to change later): `{event_id, layer, kind, subject_ref, payload, schema_version, producer, ts}`. Consumers must be idempotent on `event_id`.

### 6.2 `l1_sources`

```sql
source_families(id, key, name, scope_charter jsonb)
sources(id, family_id, key, name, kind law|regulation|standard|guidance|form|agreement,
        issuer, jurisdiction, license open|restricted, license_ref, adapter, canonical_url)
family_members(family_id, source_id, relation amends|consolidates|corrects|supplements|interprets|implements,
        tier binding|guidance|informative, status active|superseded,
        added_via citator|citation|watchlist|manual)
source_versions(id, source_id, version_label, effective_date, retrieved_at,
        s3_uri, content_hash, status in_force|superseded|revoked)
clauses(id, source_version_id, ref, path, ordering int, text, text_hash,
        public_ok bool, embedding vector(1024), embedding_model text)
citations(id, from_clause_id, raw_text, resolved_source_id null,
        disposition resolved|out_of_scope|open, reason)
discovery_candidates(id, family_id, url, title, found_via, classification jsonb,
        status proposed|accepted|rejected)           -- accept/reject happens via l0.proposals
change_events(id, source_id, kind added|amended|revoked, old_version, new_version,
        clause_refs text[], detected_at, diff_s3_uri)
licenses_held(id, product, vendor, scope, seats, purchased_at, renewal_at, notes)
byol_uploads(id, user_id, source_id, content_hash, verified bool, s3_uri, created_at)
```

View for the public web role: `clauses_public AS SELECT … FROM clauses WHERE public_ok`. The reader role has **no grant** on `clauses`.

---

## 7. Component design

### 7.1 L0 platform plane (build first)

- **Outbox + relay (`events.py`).** Writers insert into `l0_platform.events` in the same transaction as their data change; a relay loop in the worker ships unrelayed rows to SQS and stamps `relayed_at`. Nothing publishes to SQS directly. This guarantees no lost `SourceChanged`.
- **LLM gateway (`gateway.py`).** `Provider` protocol; `AnthropicProvider` implemented; `OpenAICompatProvider` stub (for future vLLM). Every call: log to `llm_calls` (model, prompt sha256, tokens, cost), enforce caps, structured-output validation, retry w/ backoff. **L1 uses the gateway only for watcher-candidate triage.**
- **Proposals + review console (`proposals.py` + UI `/review`).** One queue across layers; approve endpoint requires an authenticated user with role `maintainer` (reuse existing reg42-os auth) and records identity. Approving a `family_member` proposal writes the membership row; rejecting archives it.
- **Evals harness (`evals.py`).** Pytest-style suites registered per layer; `run_suite(suite, source)` → `eval_runs` row + JSON artifact; CLI + CI entrypoints; release gate = all suites passed for all sources at the tagged commit.
- **Exporter (`exporter.py`).** On git tag `clhear-vX.Y.Z`: compile refs/hashes/family graphs/eval scores → JSON/YAML → commit to the public `clhear` repo. Hard filter: any row/artifact with `public_ok=false` or under `restricted/` is excluded by construction (allow-list export, not deny-list).

### 7.2 L1 components

- **Adapter contract (`adapters/base.py`).** `meta() -> SourceMeta` and `fetch(since_version|None) -> (artifacts[], ClauseTree)`. ClauseTree nodes: `{ref, path, ordering, text, children[]}`. Adapters do retrieval + normalization ONLY; the pipeline owns hashing, storage, diffing, events. Politeness: identify a UA string, honor robots/ToS, backoff on 429.
- **Adapters (5):**
  1. `uk_legislation` — legislation.gov.uk XML API (CLML) for MLRs 2017; also pulls the official *effects/changes* data for the family (citator role).
  2. `eur_lex` — EUR-Lex/CELEX for GDPR (32016R0679): Formex/HTML article tree + relations metadata (amended-by, corrigenda) for the family.
  3. `govinfo_us` — govinfo + eCFR APIs: 26 USC ch.4 (statute) and 26 CFR §1.1471+ (regs) for FATCA.
  4. `irs_gov` — watcher+fetcher for the IRS administrative layer (Rev. Procs, FFI agreement, W-8BEN-E/8966 form *instructions*): index-page watcher detects items; PDFs parsed with **Docling, locally**.
  5. `restricted_file` — importer, not crawler: licensed ISO 27001(+27002) PDF and the free AICPA TSC PDF dropped into a private inbox (admin upload route), Docling-parsed to ClauseTree, stored `license=restricted`, every clause `public_ok=false`. Catalog watchers (ISO/AICPA pages) alert on new editions only.
- **Pipeline (`pipeline.py`).** For each adapter run: store artifacts → S3 (`public-ok/` or `restricted/` by license) → upsert version + clauses → clause-level diff vs previous version (align by `ref`, fallback text-similarity for renumbering) → `change_events` + outbox `SourceChanged` → enqueue embedding + citation-mining jobs.
- **Families (`families.py`).** Citator sync (official feeds → auto-proposals at `binding` tier); citation mining (regex+patterns for "as amended by", "SI 2019/…", "Article … of Regulation (EU) …", "Rev. Proc. …" → `citations` rows; unresolved → discovery candidates); reconciliation job (family graph vs citator list → any miss = candidate + eval failure).
- **Embeddings (`embeddings.py`).** BGE-M3 via sentence-transformers, CPU batch task, only new/changed clauses; write `embedding` + `embedding_model`.
- **Guard (`guard.py`).** `check(text) -> GuardReport`: max n-gram (n=8) overlap ratio + max embedding cosine vs all restricted clauses; block if overlap>0.15 or cosine>0.92 (tune in review); library call now, CI release gate always.
- **Explorer UI (`/sources`).** Library → source → version → clause tree; full-text (pg trigram) + semantic search; version diff view; **family graph view** with tier colors; freshness board; restricted sources show refs + locked verbatim pane with **BYOL**: user uploads their own licensed file, `content_hash` verified, stored under `byol/{user}/`, verbatim unlocked for that user only. Follow the Reg42 UI language (indigo/pearl/mint tokens as in the Solon reference).

### 7.3 Source corpus & family charters (v1)

| Family | Root | Binding tier | Guidance tier | Deliberately out (charter) |
|---|---|---|---|---|
| UK MLRs | MLRs 2017 (SI 2017/692) | principal SI + all amending SIs (citator feed) | JMLSG (later) | FCA speeches, consultations |
| EU GDPR | Reg. (EU) 2016/679 | regulation + corrigenda (CELEX relations) | EDPB guidelines (watcher, later) | national DPA guidance |
| US FATCA | 26 USC §§1471–1474 | statute + 26 CFR ch.4 + current FFI-agreement Rev. Proc. + W-8BEN-E/8966 instructions | IRS pubs | **IGAs: stubbed as `agreement` members at reference level only in v1** (activated per-jurisdiction when L4 exists) |
| ISO 27001 | ISO/IEC 27001:2022 | standard + Amd 1:2024 (**restricted**) | ISO 27002 (restricted) | — |
| SOC 2 | AICPA TSC 2017(w/2022 pts) | TSC (**restricted**, though freely downloadable) | AICPA mapping docs | — |
| NIST spine | SP 800-53 r5 + CSF 2.0 | full text (public domain — the open canonical infosec text) | — | — |

FATCA is sequenced **last** of the crawl adapters — it is the multi-publisher stress test of the family machinery, and its charter above is the scoped v1 answer.

### 7.4 Evals (public evidence) — E1–E7

E1 Fidelity: dual-path re-fetch of ≥10% sample (min 50 clauses), byte-exact after whitespace-normalization — target 100%.
E2 Completeness: stored clause tree vs the document's own authoritative index — reported `stored/expected`, target 100%.
E3 Round-trip: reassemble full document from DB, diff vs original — ≥99.9% similarity (the headline number).
E4 Change replay: historical amendment snapshots through the diff engine — 100% recall; plus live time-to-detect vs official publication date.
E5 Provenance: every clause → version → S3 original, hashes recomputed, Object Lock verified — 100%.
E6 Retrievability: golden queries per source, hits@5 ≥95%.
E7 Family closure: citations 100% dispositioned (0 unexplained) + family graph ⊇ official citator list + scheduled adversarial web-probe sweep (any find not in family/candidates = logged failure).
Publishing: scores per source per release → `clhear` repo `evals/`; restricted sources publish **scores without text**; open sources also publish the runner for third-party reproduction.

---

## 8. Cursor working rules

1. Read this HLD fully before the first line of code. When this HLD and older docs disagree, this HLD wins; visual questions → the .drawio.
2. Follow existing `reg42-os` conventions (migrations, settings, auth) — read neighboring code first. No new frameworks, no build step for frontend, no new ECS images.
3. Determinism rule: no LLM call outside `gateway.py`, and in this build only watcher triage may call it.
4. Restricted rule: any code path that could emit clause text must route through `clauses_public` or an explicit BYOL check. Treat a violation as a failing test, and write the tests.
5. One PR per phase (below); each PR ships its done-test as an automated test where possible.
6. Anything ambiguous: smaller change + `# ARCH:` comment for review. Never invent scope.
7. External fetching: cache aggressively in dev (recorded fixtures); never hammer official endpoints; all adapters must run green offline against fixtures.

---

## 9. Build plan (phases = PRs, each with a done-test)

**P0 — L0 rails (wk 1).** Terraform (§5, Object-Lock bucket first); `l0_platform` migration; outbox+relay; gateway with caps; proposals + `/review` UI; evals harness skeleton; exporter to a placeholder public repo. *Done-test: dummy-fleet rehearsal — event through outbox→SQS→worker, one gateway call logged with cost, proposal approved with identity, downstream event emitted, tag exports an empty-but-valid snapshot with green (empty) evals.*
**P1 — Contract + first adapter (wk 1–2).** `l1_sources` migration; adapter contract + fixture adapter; `uk_legislation` incl. effects-feed citator sync; pipeline (hash/version/diff/events). *Done-test: MLRs fully ingested; replayed historical amendment yields correct clause-diff + `SourceChanged`; family auto-contains the amending SIs from the citator.*
**P2 — Breadth (wk 2–3).** `eur_lex` (+relations), `govinfo_us`, NIST spine ingest; citation mining + reconciliation; embeddings job. *Done-test: E2 100% on GDPR; semantic search returns MLR reg 27–28 for "customer due diligence"; citations table populated with 0 unexplained on MLRs.*
**P3 — Restricted + IRS layer (wk 3).** `restricted_file` importer (buy ISO 27001+27002 ≈$350; TSC free) + BYOL flow; `irs_gov` watcher/fetcher with Docling; guard library. *Done-test: anonymous user sees GDPR verbatim but only ISO refs; BYOL unlock works for one user; paraphrased-ISO text blocked by guard, original obligation text passes.*
**P4 — Evidence + ship (wk 4–4½).** E1–E7 implemented, wired to CI + weekly schedule; `/sources` Explorer complete (family graph, freshness, diffs); dashboards + alarms; first tagged release `clhear-v0.1.0` exports scorecards to the public repo; IP-lawyer review of crosswalk/guard before the repo goes public. *Done-test: full acceptance rehearsal — all adapters green on freshness board, E1–E7 green and published, restricted discipline demonstrated end-to-end.*

Budget: ≈$350 one-time licenses; ≈$5–10/month AWS marginal; LLM spend capped at $100/day and expected near-zero in this build.

**Definition of done for the whole HLD:** a stranger can open clhear.reg42.ai, browse six source families with verbatim open texts, watch a change land as a clause-level diff, click from any clause to its immutable original, read a published scorecard proving fidelity/completeness/closure — and reproduce those scores themselves from the public repo. That is the foundation L2 builds on.

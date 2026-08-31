# CLHEAR — user & developer manual

CLHEAR is an open compliance stack: verbatim regulatory truth at the bottom
(L1), machine-derived obligations above it (L2), curated building blocks and
activities (L3–L5), and deterministic program composition and risk scoring
(L6–L7) — every claim traceable to the exact clause that justifies it.

Live at **https://clhear.reg42.ai**.

## 1. Browsing the stack

- **Home ( / )** — the nine-layer stack. Status LEDs are the honesty model:
  `LIVE` (real, hash-verified), `AI-GENERATED` / `DERIVED` (machine or routed-AI,
  with audit coverage %), `CURATED` (seeded human catalog + fleet rows),
  `COMPUTED` (deterministic engine output), `LOCKED` (definitions only).
- **Every layer page** explains *how it knows what it knows*: inputs, method,
  fit-to-purpose generation contract (nature, technique, structural guarantee,
  may/must-not, eval gates), and inspectable evidence.
- **Team ( #/team )** — AI fleet personas (Scout, Miner, Weaver, Mason, Surveyor…)
  plus human contributors, with live model/run stats.
- **AI Ops ( #/ops )** — chronological feed of router decisions, fleet writes,
  revalidation verdicts, GPU lifecycle — each with a reasoning field.
- **Eval Studio ( #/eval )** — sampled human-vs-AI tasks; agreement scores
  publish on /how and feed the router quality table.
- **Lineage** — click any item, anywhere: the drawer walks its derivation
  chain down to the verbatim clause, with sha256 hashes, retrieval time and
  the immutable original.
- **Sources Explorer ( /sources )** — the full L1 corpus: families, document
  reader, clause inspector, fleet board (with per-source schedule status),
  change events, and E1–E7 evidence.

## 2. Contributing

Sign in with email (magic link), Google, or Apple — no GitHub needed.

- **Open a case**: missing data, a correction, a new source to cover, a
  product suggestion — from the Contribute page or the "flag" button on any
  item. Cases are reviewed by a named maintainer; accepted cases credit you
  on the contributors wall.
- **Validate outputs**: on any derived obligation, Confirm ("this is a real
  obligation, correctly anchored") or Dispute. Three confirms with no
  disputes suggest promotion; promotion itself is always a recorded human
  decision.
- Contributions are licensed CC BY 4.0 with a DCO-style attestation
  (see /terms). Never paste text from licensed standards.

## 3. The API (v1)

Auth: per-app key — send `X-App-Id` and `Authorization: Bearer <secret>`.
Request a key: open a `product_suggestion` case or email api@reg42.ai.
Interactive schema: `https://clhear.reg42.ai/docs` (OpenAPI).

```bash
BASE=https://clhear.reg42.ai/v1
H1="X-App-Id: os-dev"; H2="Authorization: Bearer <your-secret>"

# Feature detection: the 8-layer contract with statuses
curl -s -H "$H1" -H "$H2" $BASE/layers

# Latest release + L1 resources
curl -s -H "$H1" -H "$H2" $BASE/releases/latest
curl -s -H "$H1" -H "$H2" "$BASE/releases/<id>/l1/clauses?q=due+diligence"

# The derived obligation registry (layer_status: derived)
curl -s -H "$H1" -H "$H2" $BASE/releases/<id>/l2/obligations
```

### The blueprint endpoint

`POST /v1/blueprint` — a compliance-program blueprint tailored to a business
profile. Attributes follow the L4 schema (see `/v1/layers`):

```bash
curl -s -X POST -H "$H1" -H "$H2" -H "content-type: application/json" \
  $BASE/blueprint -d '{
    "attributes": {
      "jurisdictions": ["EU"],
      "authorisations": ["CASP (MiCA)"],
      "products": ["crypto custody"],
      "customer_base": ["retail"],
      "data_footprint": "large-scale personal data",
      "crypto_services": true,
      "financial_entity_dora": true
    },
    "activities": null
  }'
```

The response contains: triggered obligations (with basis clause refs +
hashes and their `derived`/`validated` status), recommended building blocks,
a coverage matrix with **explicit gaps**, unmapped-obligation counts for your
jurisdictions, engine version, release pin, and the legal block (per-source
attributions + disclaimer). Same profile + same release ⇒ same blueprint.

## 4. Freshness

The whole stack refreshes nightly at **00:00 UTC**: L1 adapters fetch every
scheduled source (per-source status on the fleet board — a promised run that
did not happen shows as `schedule-missed` and trips an alarm). An ephemeral
g6.xlarge spot GPU (4h fuse, 5h orphan alarm) may come up for local-large
Qwen. Then the AI fleets run: L2 duty-triage (evidence-span contract) and
auto-applied consolidation, L3 block generation, L4 grounded license RAG,
L5 activity mapping, L6 citation-checked rationale, L7 number-echo narratives,
L8 k≥5 cohorts (no LLM). Every LLM call goes through `router.run(task_id)` —
cheapest sufficient tier; classification never leaves CPU; frontier is capped
at $50/month. Eval suites are gates (including `l4_grounding`, `l6_citation`,
`l7_number_echo`, `l8_k_anonymity`). The public snapshot is republished.

## 5. Self-hosting / development

```bash
pip install -r requirements.txt
python3 -m pytest tests/ -q          # offline, fixture-driven
uvicorn app.main:app --reload        # http://127.0.0.1:8000
CLHEAR_AUTH_DEBUG=true               # magic links returned in the response (no SES)
```

Key environment variables: `DATABASE_URL` (SQLite default), `CLHEAR_APP_KEYS`
(`app:secret[,scopes]`), `CLHEAR_SESSION_SECRET`, `GOOGLE_OAUTH_CLIENT_ID/SECRET`,
`CLHEAR_EVENTS_QUEUE_URL` + `CLHEAR_DB_S3_URI` (read-only snapshot mode).

## 6. Legal

See `/disclaimer` and `/terms`. Per-source attribution ships with every
document view and API payload. Restricted standards are refs + hashes only.

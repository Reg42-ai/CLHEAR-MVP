# CLHEAR → reg42-infra handoff

CLHEAR performs no inference of its own (HLD v2 invariant I6, §9). Every model
call goes through **Reg42 Infer** (`https://infer.reg42.ai/v1`, OpenAI-compatible)
which fronts Amazon Bedrock inside the Reg42 account.

## What reg42-infra needs from this folder

| File | Purpose |
|---|---|
| `tasks.clhear.yaml` | The CLHEAR task classes, their procurement-clean model ladders, thresholds and default data class. Generated from `app/clhear/platform/task_classes.py`; a CI test keeps the two byte-identical. Load it into Infer's routing (`policy.yaml` routes + `models.yaml` tiers) for employee `clhear`. |

## Request contract (what CLHEAR sends)

```
POST {INFER_BASE_URL}/chat/completions
Authorization: Bearer <INFER_TOKEN>
X-Employee-Id: clhear               # one Infer employee for all fleets (see Status below)
X-Task-Class: <task class>          # from tasks.clhear.yaml
X-Data-Class: public | members | restricted
{ "model": "<bedrock model id>", "messages": [...], "max_tokens": n,
  "temperature": 0.0, "response_format": {...},
  "metadata": {"task_class": "...", "employee_id": "..."} }
```

Infer may fall through the ladder; the response `model` field must be the Bedrock
model id that actually ran (CLHEAR records it in the ledger and freezes it in the
release model manifest). `usage.cost_usd`, when present, overrides CLHEAR's
price table.

```
GET {INFER_BASE_URL}/route/explain?task_class=<tc>&employee_id=<id>
→ {"task_class": "...", "ladder": ["...", "..."], "selected": "..."}
```

Used nightly by `python -m app.clhear.platform.manifest freeze <release>` to freeze
the model ids for a release.

## Policy Infer must enforce for employee `clhear`

* Derivation classes (`derivation: true`) never route to a model whose origin is
  `CN`; if every clean rung fails the call fails — no silent fallback.
* `hosting: aws-bedrock` only. No external vendor endpoints.
* Budget: the CLHEAR gateway enforces its own daily/monthly caps; Infer should
  additionally cap the `clhear` employee (`daily_usd_cap` in `catalog/employees.yaml`).

## Secrets

* SSM `/clhear/INFER_TOKEN` (SecureString) — created by `infra/ssm.tf`, value set by
  the Reg42 Infer operator.

## Status against the live Infer (checked 13 Sep 2026)

Verified against `https://infer.reg42.ai` (`reg42-infer-all`, task definition
`workforce-dev-infer:5`) by reading its `app/tokens.py`, `app/main.py` and
`catalog/*.yaml` from the deployed image.

**Identity — done.** Infer tokens are HMAC-derived per employee
(`infer-v2.<employee>.<exp>.<sig>`, `INFER_TOKEN_REQUIRE_TTL=1` so only expiring v2
tokens verify) and a token is bound to exactly one employee id: `X-Employee-Id`
must equal it or Infer answers 403. CLHEAR therefore uses **one employee,
`clhear`**, for every fleet and the web tier (`infra/ecs.tf`); the layer is in
CLHEAR's own ledger. A v2 token for `clhear` (TTL 365 d, expires 2027-09-13) is
in `/clhear/INFER_TOKEN`; `GET /v1/models` with it returns 200. Re-mint before
expiry with `tokens.mint(secret, "clhear", ttl_seconds=…)` (`scripts/mint-infer-token.py`);
`rotate_all` does not cover CLHEAR because it writes Secrets Manager seats, not this SSM
parameter. Add `clhear` to `catalog/employees.yaml` with a `daily_usd_cap` so Infer caps it.

**Routing — open, blocks every derivation.** Infer routes by `X-Task-Class`
→ `policy.yaml` route → tier → champion in `models.yaml` and overwrites the
requested `model`. No route matches the CLHEAR classes, so they fall to the
`default` route, tier `classify`, champion `qwen3-32b` (observed live:
`l2_extract` → `qwen3-32b`, `route_id=default`). None of the ladder models in
`tasks.clhear.yaml` are in `models.yaml`. CLHEAR's gateway now **fails closed**
when a derivation class is answered by a model outside the clean set
(`app/clhear/platform/gateway.py::_assert_procurement_clean`), so until the
routes exist every CLHEAR derivation call errors and the nightly stack derives
nothing. Required in reg42-infra:

1. `catalog/models.yaml`: add the ladder models with their Bedrock ids
   (`amazon.nova-lite-v1:0`, `amazon.nova-pro-v1:0`, `openai.gpt-oss-120b-1:0`,
   `mistral.mistral-large-3-v1:0`, `anthropic.claude-sonnet-5-v1:0`,
   `anthropic.claude-opus-5-v1:0`, `meta.llama3-3-70b-instruct-v1:0`; embeddings
   `amazon.titan-embed-text-v2:0` already exists) and enable them in Bedrock
   model access for us-east-1.
2. `catalog/models.yaml` tiers: one per CLHEAR ladder, e.g. `clhear-derivation`
   (champion `gpt-oss-120b`, fallbacks `mistral-large-3`, `claude-sonnet-5`,
   `claude-opus-5`), `clhear-hard`, `clhear-cheap`, `clhear-nonderivation` — the
   exact ladder per class is in `tasks.clhear.yaml`.
3. `catalog/policy.yaml` routes: `match.task_class` = the class ids from
   `tasks.clhear.yaml` → the matching tier. Derivation tiers must contain no
   CN-origin fallback; when the tier is exhausted the call fails.
4. Response `model`: return the Bedrock id (or include it, e.g. `infer.bedrock_id`)
   for `clhear`; CLHEAR's ledger, price table and release manifest key on Bedrock ids.
   Catalog short names of clean families (`claude-*`, `llama*`, `nova*`, `titan*`,
   `gpt-oss*`, `mistral*`) are accepted by the origin check in the meantime.

**Endpoints — open, degrade gracefully.**

* `POST /v1/embeddings` (OpenAI-compatible) does not exist; CLHEAR's
  `InferEmbedder` calls it for task class `embed`. Until it exists the vector
  index is built with the offline `hash-v1` embedder and labelled as such.
* `GET /v1/route/explain?task_class=&employee_id=` does not exist; the release
  manifest falls back to the first rung of CLHEAR's default ladder.

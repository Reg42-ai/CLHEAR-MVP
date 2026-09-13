# CLHEAR → reg42-infra handoff

CLHEAR performs no inference of its own (HLD v2 invariant I6, §9). Every model
call goes through a **Reg42 Infer** router (OpenAI-compatible) that fronts Amazon
Bedrock inside the Reg42 account.

CLHEAR is a product, not a workforce seat, so it does not sit in the workforce
roster and its spend never draws on the employees' company-wide monthly ceiling
(which the shared Infer enforces over every caller). Since 13 Sep 2026 it runs
**`clhear-infer`**: the same Infer image pinned by digest, with CLHEAR's own
catalog, token secret, spend ledger and caps, reachable only inside CLHEAR's VPC
(`http://infer.clhear.local:8000/v1`). The shared `https://infer.reg42.ai` is no
longer in CLHEAR's path.

## What reg42-infra needs from this folder

| File | Purpose |
|---|---|
| `tasks.clhear.yaml` | The CLHEAR task classes, their procurement-clean model ladders, thresholds and default data class. Generated from `app/clhear/platform/task_classes.py`; a CI test keeps the two byte-identical. |
| `../../infra/infer-catalog/` | The complete Infer catalog CLHEAR runs today (`models.yaml`, `policy.yaml`, `employees.yaml`, `budget.yaml`, `tools.yaml`, `Dockerfile`). Rendered from the same module by `scripts/render_infer_catalog.py`; a CI test keeps it current. This is the input to `kernel/infer`'s task-class registry when it takes the CLHEAR contract natively. |

## Request contract (what CLHEAR sends)

```
POST {INFER_BASE_URL}/chat/completions
Authorization: Bearer <INFER_TOKEN>      # infer-v2 token for principal `clhear`
X-Employee-Id: clhear                    # Infer's name for the principal; one for all fleets and web
X-Task-Class: <task class>               # from tasks.clhear.yaml
X-Data-Class: public | members | restricted
{ "model": "<bedrock model id>", "messages": [...], "max_tokens": n,
  "temperature": 0.0, "response_format": {...},
  "metadata": {"task_class": "...", "employee_id": "..."} }
```

Infer picks the model from the route's tier and may fall through the ladder; the
response `model` must be the Bedrock id that ran. `clhear-infer` guarantees this
by keying its catalog entries by Bedrock id. `usage.cost_usd`, when present,
overrides CLHEAR's price table.

```
GET {INFER_BASE_URL}/route/explain?task_class=<tc>&employee_id=<id>
→ {"task_class": "...", "ladder": ["...", "..."], "selected": "..."}
```

Used nightly by `python -m app.clhear.platform.manifest freeze <release>`; when the
endpoint is absent the manifest freezes the first rung of CLHEAR's default ladder.

## Policy the router enforces for principal `clhear`

* Derivation classes (`derivation: true`) never reach a model whose origin is `CN`.
  In `clhear-infer` no such model exists in the catalog, so Infer's last-resort
  picker cannot select one either; CLHEAR's gateway additionally fails closed if a
  derivation answer names a model outside the clean set
  (`gateway.py::_assert_procurement_clean`).
* `hosting: aws-bedrock` only. No external vendor endpoints, no tools.
* Budget: `employees.yaml` `daily_usd_cap: 250`; `budget.yaml` `hired_usd: 2000`
  is the monthly hard stop. CLHEAR's own gateway caps (`settings.py`) apply on top.
  Spend is in the `infer` database on CLHEAR's Aurora cluster.

## Models (verified in the CLHEAR account, us-east-1, 13 Sep 2026)

| Bedrock id | Origin | Use |
|---|---|---|
| `openai.gpt-oss-120b-1:0` | US | derivation champion |
| `mistral.mistral-large-3-675b-instruct` | EU | derivation fallback, hard champion |
| `us.anthropic.claude-opus-4-5-20251101-v1:0` | US | hard fallback (premium cap) |
| `amazon.nova-lite-v1:0`, `amazon.nova-pro-v1:0` | US | cheap / non-derivation |
| `us.meta.llama3-3-70b-instruct-v1:0` | US | non-derivation fallback |
| `amazon.titan-embed-text-v2:0` | US | embeddings (1024) |

Not available yet: `anthropic.claude-sonnet-5` / `claude-opus-5` ("not available
for this account", AWS sales) and every other Anthropic model until the account's
**Anthropic use-case form** is accepted in the Bedrock console. `CLAUDE_SONNET`
(`us.anthropic.claude-sonnet-4-5-20250929-v1:0`) is priced and origin-tagged but in
no default ladder and `enabled: false` in the catalog until then.

## Secrets (all SSM SecureString, created by Terraform)

* `/clhear/INFER_TOKEN_SECRET` — HMAC key of `clhear-infer`. Rotating it invalidates
  every token; re-mint afterwards.
* `/clhear/INFER_TOKEN` — the `clhear` principal's infer-v2 token (TTL 365 d,
  expires 2027-09-13). Mint: `tokens.mint(secret, "clhear", ttl_seconds=…)` with the
  secret above; the format is `infer-v2.<id>.<exp>.<base32(HMAC-SHA256(secret, "infer-v2:"+id+":"+exp)[:15])>`.
* `/clhear/INFER_DATABASE_URL` — spend ledger DSN (`infer` database on Aurora).

## Operating `clhear-infer`

* Image: `infra/infer-catalog/Dockerfile` = `FROM workforce-dev-infer@sha256:3316…`
  + `COPY catalog`. Build and push with `scripts/build_worker_image.sh infer`
  (CodeBuild project `clhear-infer-image`, pushes `clhear-infer:latest`); the ECS
  service `clhear-infer` picks it up on the next deployment (`terraform apply`).
* Change routing or caps by editing `task_classes.py` (ladders) or the render
  arguments (`--daily`, `--monthly`), running `scripts/render_infer_catalog.py`,
  rebuilding the image, applying. Never hand-edit the catalog: the test fails.
* Upgrade Infer by bumping `INFER_IMAGE` in `app/clhear/platform/infer_catalog.py`
  to a new digest, re-rendering, rebuilding. Read the new image's `policy.py`
  and `catalog.py` first; the catalog schema is theirs.
* Known gaps in the current image: no `POST /v1/embeddings` and no
  `GET /v1/route/explain` (manifest falls back as above). Both are requests on
  `kernel/infer`. Until the first lands, the clause vector index is built by
  `BedrockEmbedder` (`platform/embeddings.py`) calling Titan Text Embeddings v2
  directly; the worker and web roles may invoke only the two embedding model ARNs
  (`infra/iam.tf` `EmbeddingModels`), the never-list test names this single
  exemption, and an embedding is a rebuildable projection — nothing it produces
  enters the record. Set `CLHEAR_EMBEDDING_PROVIDER=infer` once the route exists.

## When `kernel/infer` ships the CLHEAR contract natively

1. Load `infra/infer-catalog/` (or `tasks.clhear.yaml`) into its task-class registry
   under principal `clhear`, keeping Bedrock ids in the response `model`.
2. Give CLHEAR its own budget line, not a workforce seat.
3. Add `/v1/embeddings` and `/v1/route/explain`.
4. Then in CLHEAR: `infer_private_enabled = false`, `infer_base_url` = the new URL,
   re-mint `/clhear/INFER_TOKEN` against the new secret, `terraform apply`.

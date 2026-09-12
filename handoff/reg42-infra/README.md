# CLHEAR → reg42-infra handoff

CLHEAR performs no inference of its own (HLD v2 invariant I6, §9). Every model
call goes through **Reg42 Infer** (`https://infer.reg42.ai/v1`, OpenAI-compatible)
which fronts Amazon Bedrock inside the Reg42 account.

## What reg42-infra needs from this folder

| File | Purpose |
|---|---|
| `tasks.clhear.yaml` | The CLHEAR task classes, their procurement-clean model ladders, thresholds and default data class. Generated from `app/clhear/platform/task_classes.py`; a CI test keeps the two byte-identical. Load it into Infer's task-class registry under the `clhear-` employee prefix. |

## Request contract (what CLHEAR sends)

```
POST {INFER_BASE_URL}/chat/completions
Authorization: Bearer <INFER_TOKEN>
X-Employee-Id: clhear-l<n>          # one identity per layer fleet
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

## Policy Infer must enforce for `clhear-*`

* Derivation classes (`derivation: true`) never route to a model whose origin is
  `CN`; if every clean rung fails the call fails — no silent fallback.
* `hosting: aws-bedrock` only. No external vendor endpoints.
* Budget: the CLHEAR gateway enforces its own daily/monthly caps; Infer should
  additionally cap `clhear-*` employees at the agreed monthly figure.

## Secrets

* SSM `/clhear/INFER_TOKEN` (SecureString) — created by `infra/ssm.tf`, value set by
  the Reg42 Infer operator.

# CLHEAR — MVP

Live, always-on regulatory-truth system. This repo builds the foundation from the
CLHEAR HLD: **L0 platform rails** (event outbox, LLM gateway with spend caps, human
review console, evals harness, public exporter) and, in later phases, **L1 verbatim
sources** (adapters, clause pipeline, families, embeddings, restricted-zone guard).

## Status

| Phase | Scope | State |
|---|---|---|
| P0 | L0 rails: outbox+relay, gateway+caps, proposals + `/review`, evals harness, exporter, Terraform | this branch |
| P1 | `l1_sources`, adapter contract, `uk_legislation` + citator, pipeline | planned |
| P2 | `eur_lex`, `govinfo_us`, NIST spine, citation mining, embeddings | planned |
| P3 | Restricted importer + BYOL, `irs_gov` + Docling, similarity guard | planned |
| P4 | E1–E7 evals, `/sources` Explorer, dashboards, first release | planned |

## Run locally

```bash
pip install -r requirements.txt
pytest tests/ -v                       # includes the P0 dummy-fleet rehearsal
uvicorn app.main:app --reload          # /review console, /api/clhear/*
```

Defaults use SQLite; set `DATABASE_URL` for Postgres (Aurora). Feature flag:
`REG42_CLHEAR_ENABLED`.

## Worker

```bash
python -m app.clhear.workers           # SQS consumer + outbox relay (ECS clhear-workers)
```

## Evals and release

```bash
python -m app.clhear.platform.evals all clhear-v0.1.0
python -m app.clhear.platform.exporter clhear-v0.1.0        # gated: refuses if evals not green
```

A `clhear-vX.Y.Z` git tag runs the same gate in CI and exports the public snapshot.

## Infrastructure

Additive Terraform in [`infra/`](infra/): Object-Lock (compliance) S3 datalake,
`clhear-events` SQS + DLQ, EventBridge adapter schedules, `clhear-workers` on
Fargate Spot (min 0 / max 2, scales on queue depth), SSM parameters, CloudWatch
dashboard + alarms. Variables accept existing reg42-infra VPC/cluster/ALB IDs;
without them a minimal self-contained stack is created.

```bash
terraform -chdir=infra init && terraform -chdir=infra apply
```

## Principles (from the HLD)

Verbatim ground truth, determinism first (LLM only via the gateway, review-gated),
named-human approval gates, restricted-zone discipline, evals as release gates,
near-zero idle cost, full replayability.

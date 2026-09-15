# Infrastructure

This document describes the infrastructure setup for the CLHEAR MVP repository.

## AWS Resources

- **ECS Clusters**: `clhear-workers` (SQS consumer + outbound relay, EC2 clhear-worker)
- **Database**: SQLite default; set `DATABASE_URL` for PostgreSQL (Aurora).
- **Feature Flag**: `REG42_CLHEAR_ENABLED`.

## Working Locally

```bash
pip install -r requirements.txt
pytest tests/ -v                          # includes the P0 dump-fleet rehearsal
uvicorn app.main:app --reload             # /review console, /api/clhear/*
```

## Evolutions and Releases

```bash
python -m app.clhear.platform.evals all clhear-v0.1.0
python -m app.clhear.platform.exporter clhear-v0.1.0   # gated: refuses if evals not green
```

A `clhear-vX.Y.Z` git tag runs the same gate in CI and exports the public snapshot.

## Feedback

For infrastructure feedback, open an issue or ping `@eng` in `#eng`.
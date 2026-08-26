"""Seed the eToro L1 blueprint into the corpus (thin CLI).

Data + logic live in app/clhear/l1/registry_etoro.py so the same plan drives
seeding, Wave-1 ingestion, and the daily fleet.

Usage: DATABASE_URL=sqlite:///deploy/clhear.db python scripts/seed_registry.py
"""
import json
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clhear.db import get_engine, run_migrations  # noqa: E402
from app.clhear.l1 import registry_etoro  # noqa: E402


def main() -> int:
    engine = get_engine()
    run_migrations(engine)
    print(json.dumps(registry_etoro.seed(engine)))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

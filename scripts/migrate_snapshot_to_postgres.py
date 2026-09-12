"""Copy the SQLite snapshot (single-writer MVP store) into Aurora Postgres.

HLD v2 I7: Postgres is the record. This one-shot script runs the numbered
migrations on the target, then copies every layer table row-for-row in
layer order (L0 → L8) so foreign keys resolve. Idempotent: rows whose primary
key already exists in the target are skipped, and nothing is ever deleted (I2).

    python scripts/migrate_snapshot_to_postgres.py ./clhear-latest.db \
        postgresql+psycopg://user:pass@host:5432/clhear [--dry-run]
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

import sqlalchemy as sa

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clhear.db import make_engine, run_migrations  # noqa: E402
from app.clhear.platform.record import layer_tables  # noqa: E402

BATCH = 500


def _pk_cols(table: sa.Table) -> list[sa.Column]:
    return list(table.primary_key.columns)


def copy_table(src, dst, table: sa.Table, dry_run: bool) -> dict:
    pk = _pk_cols(table)
    with src.connect() as sconn:
        rows = [dict(r._mapping) for r in sconn.execute(sa.select(table))]
    if not rows:
        return {"table": table.name, "source_rows": 0, "copied": 0, "skipped": 0}
    existing: set = set()
    if pk:
        with dst.connect() as dconn:
            existing = {tuple(r) for r in dconn.execute(sa.select(*pk))}
    to_copy = [r for r in rows if not pk or tuple(r[c.name] for c in pk) not in existing]
    if not dry_run and to_copy:
        with dst.begin() as dconn:
            for i in range(0, len(to_copy), BATCH):
                dconn.execute(table.insert(), to_copy[i : i + BATCH])
    return {
        "table": table.name,
        "source_rows": len(rows),
        "copied": len(to_copy),
        "skipped": len(rows) - len(to_copy),
    }


def reset_sequences(dst) -> None:
    """Integer PKs were copied verbatim; move each Postgres sequence past them."""
    with dst.begin() as conn:
        for table in layer_tables():
            for col in _pk_cols(table):
                if isinstance(col.type, sa.Integer) and col.autoincrement in (True, "auto"):
                    seq = conn.execute(
                        sa.text("select pg_get_serial_sequence(:t, :c)"),
                        {"t": f"{table.schema}.{table.name}", "c": col.name},
                    ).scalar()
                    if seq:
                        conn.execute(
                            sa.text(f"select setval('{seq}', coalesce((select max({col.name}) from {table.schema}.{table.name}), 0) + 1, false)")
                        )


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("snapshot", help="path to the SQLite snapshot")
    parser.add_argument("target", help="postgresql+psycopg DSN")
    parser.add_argument("--dry-run", action="store_true")
    args = parser.parse_args()

    if not args.target.startswith("postgresql"):
        print("target must be a postgresql DSN", file=sys.stderr)
        return 2
    src = make_engine(f"sqlite:///{args.snapshot}")
    dst = make_engine(args.target)
    run_migrations(dst)

    results = [copy_table(src, dst, table, args.dry_run) for table in layer_tables()]
    if not args.dry_run:
        reset_sequences(dst)
    print(json.dumps({"dry_run": args.dry_run, "tables": results}, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

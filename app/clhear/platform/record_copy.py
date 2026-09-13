"""Copy the whole record between two engines (SQLite <-> Postgres).

Two uses (HLD v2 §3, item 1 Aurora cutover):

* ``load``: the v1 SQLite snapshot into a freshly migrated Aurora database, once.
* ``export_sqlite_snapshot``: Postgres -> a fresh SQLite file, nightly, so releases
  keep shipping ``l1/snapshot.db`` and the public explorer can read it without a
  database connection.

The copy walks the *declared* tables (every ``sa.Table`` the application defines), not a
reflection of the source, so JSON, vector and datetime columns are serialised by the same
SQLAlchemy types on both sides. Tables are copied in foreign-key order. Rows are upserted
on the primary key: the destination is always a freshly migrated database whose only rows
are the migrations' own seeds, and the source row wins where both hold the same id. Nothing
is ever deleted (I2) — the extra audit and why-trail rows the migrations wrote on the
destination remain, and say so.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine
from sqlalchemy.sql.ddl import sort_tables

log = logging.getLogger("clhear.record_copy")

BATCH = 2000

# The clause vector index (HLD v2 I7) is stored as packed float32 on SQLite and as
# pgvector on Aurora; it is rebuilt from the record text (platform.embeddings), so a
# cross-dialect copy leaves it null instead of translating it.
INDEX_COLUMNS = frozenset({"embedding", "embedding_model", "embedding_hash", "embedded_at"})


def declared_tables() -> list[sa.Table]:
    """Every table the application declares, in foreign-key order."""
    # Importing registers the tables on their MetaData objects.
    from app.clhear import community_models, conformance, derived_models, models
    from app.clhear.l1 import models as l1_models
    from app.clhear.l7 import models as l7_models
    from app.clhear.l8 import models as l8_models
    from app.clhear.platform import audit, dr, ids, record  # noqa: F401  (tables on models.metadata)

    seen: dict[str, sa.Table] = {}
    for md in (models.metadata, l1_models.metadata, derived_models.metadata, l7_models.metadata,
               l8_models.metadata, community_models.metadata, conformance.metadata):
        for t in md.tables.values():
            seen.setdefault(t.key, t)
    return list(sort_tables(seen.values()))


@dataclass
class CopyReport:
    tables: dict[str, tuple[int, int]] = field(default_factory=dict)  # name -> (source rows, dest rows after)
    skipped: list[str] = field(default_factory=list)  # destination lacks the table: a real problem
    absent: list[str] = field(default_factory=list)  # source predates the table: nothing to copy
    index_reset: list[str] = field(default_factory=list)  # vector index columns left null; rebuild_index refills

    @property
    def ok(self) -> bool:
        return all(dst >= src for src, dst in self.tables.values()) and not self.skipped

    def summary(self) -> dict:
        return {
            "ok": self.ok,
            "tables": len(self.tables),
            "rows": sum(src for src, _ in self.tables.values()),
            "short": {n: c for n, c in self.tables.items() if c[1] < c[0]},
            "skipped": self.skipped,
            "absent_on_source": len(self.absent),
            "index_reset": self.index_reset,
        }


def _table_exists(conn, table: sa.Table) -> bool:
    insp = sa.inspect(conn)
    schema = table.schema
    if conn.dialect.name == "sqlite":
        schema = None
    return insp.has_table(table.name, schema=schema)


def _upsert(dialect: str, table: sa.Table, rows: list[dict]):
    pk = [c for c in table.primary_key.columns]
    if not pk:
        return table.insert(), rows
    set_ = [c.name for c in table.columns if c not in pk]
    if dialect == "postgresql":
        from sqlalchemy.dialects.postgresql import insert as pg_insert

        stmt = pg_insert(table)
        stmt = stmt.on_conflict_do_update(index_elements=pk, set_={k: stmt.excluded[k] for k in set_}) if set_ \
            else stmt.on_conflict_do_nothing(index_elements=pk)
        return stmt, rows
    from sqlalchemy.dialects.sqlite import insert as sqlite_insert

    stmt = sqlite_insert(table)
    stmt = stmt.on_conflict_do_update(index_elements=pk, set_={k: stmt.excluded[k] for k in set_}) if set_ \
        else stmt.on_conflict_do_nothing(index_elements=pk)
    return stmt, rows


def copy_record(source: Engine, dest: Engine, *, batch: int = BATCH) -> CopyReport:
    """Upsert every declared table from ``source`` into ``dest`` (already migrated)."""
    report = CopyReport()
    tables = declared_tables()
    with source.connect() as src, dest.connect() as dst:
        for table in tables:
            if not _table_exists(src, table):
                report.absent.append(table.key)
                continue
            if not _table_exists(dst, table):
                report.skipped.append(f"{table.key}: absent on destination (migrate first)")
                continue
            # A source behind on migrations lacks newer columns; copy the intersection and
            # let the destination defaults fill the rest (the source is migrated first anyway).
            src_cols = {c["name"] for c in sa.inspect(src).get_columns(table.name, schema=None if src.dialect.name == "sqlite" else table.schema)}
            cross_dialect = src.dialect.name != dst.dialect.name
            cols = [c.name for c in table.columns if c.name in src_cols and not (cross_dialect and c.name in INDEX_COLUMNS)]
            if cross_dialect and any(c.name in INDEX_COLUMNS for c in table.columns):
                report.index_reset.append(table.key)
            n_src = 0
            result = src.execution_options(stream_results=True).execute(sa.select(*[table.c[c] for c in cols]))
            while True:
                chunk = result.fetchmany(batch)
                if not chunk:
                    break
                rows = [dict(zip(cols, r)) for r in chunk]
                stmt, rows = _upsert(dst.dialect.name, table, rows)
                dst.execute(stmt, rows)
                dst.commit()
                n_src += len(rows)
            n_dst = int(dst.execute(sa.select(sa.func.count()).select_from(table)).scalar() or 0)
            dst.commit()
            report.tables[table.key] = (n_src, n_dst)
            log.info("copied %-40s %8d rows (dest now %d)", table.key, n_src, n_dst)
        if dst.dialect.name == "postgresql":
            _advance_sequences(dst, tables)
    return report


def _advance_sequences(conn, tables: list[sa.Table]) -> None:
    """Integer autoincrement keys keep counting after the copied rows."""
    for table in tables:
        for col in table.primary_key.columns:
            if isinstance(col.type, sa.Integer) and col.autoincrement in (True, "auto") and len(table.primary_key.columns) == 1:
                qualified = f"{table.schema}.{table.name}" if table.schema else table.name
                seq = conn.execute(sa.text("SELECT pg_get_serial_sequence(:t, :c)"), {"t": qualified, "c": col.name}).scalar()
                if seq:
                    conn.execute(sa.text(
                        f"SELECT setval('{seq}', COALESCE((SELECT MAX({col.name}) FROM {qualified}), 0) + 1, false)"
                    ))
                conn.commit()


def export_sqlite_snapshot(source: Engine, dest_path: str | Path) -> CopyReport:
    """Postgres (or any) record -> a fresh, fully migrated SQLite file at ``dest_path``."""
    from app.clhear.db import make_engine, run_migrations

    dest_path = Path(dest_path)
    if dest_path.exists():
        raise FileExistsError(f"refusing to overwrite {dest_path}; export to a new path and swap atomically")
    dest_path.parent.mkdir(parents=True, exist_ok=True)
    dest = make_engine(f"sqlite:///{dest_path}")
    try:
        run_migrations(dest)
        report = copy_record(source, dest)
        with dest.connect() as c:
            c.execute(sa.text("VACUUM"))
    finally:
        dest.dispose()
    return report

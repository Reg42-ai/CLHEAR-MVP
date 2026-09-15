"""Compile a private L1 release from explicitly allowed corpus tables.

Called by L0 publication only. Never copy the operational database (sessions,
model prompts, credentials and unrelated layers are not release artifacts).
"""
import os
from pathlib import Path

import sqlalchemy as sa

from app.clhear.db import make_engine
from app.clhear.l1 import models, permissions

TABLE_NAMES = ("source_families", "sources", "family_members", "source_versions",
               "doc_nodes", "clauses", "clause_annotations", "citations", "rights_records")


def current_bindings(conn) -> list[dict]:
    return [dict(r) for r in conn.execute(sa.select(
        models.sources.c.key.label("source_key"), models.source_versions.c.id.label("source_version_id"),
        models.source_versions.c.content_hash,
    ).join(models.source_versions, models.source_versions.c.source_id == models.sources.c.id)
      .where(models.source_versions.c.status == "in_force").order_by(models.sources.c.key, models.source_versions.c.id)).mappings()]


def compile_snapshot(engine, destination: Path) -> dict:
    """Read one repeatable database view and publish no unapproved text."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    descriptor = os.open(destination, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    os.close(descriptor)
    target = make_engine(f"sqlite:///{destination}")
    try:
        with engine.connect() as connection:
            if engine.dialect.name == "postgresql":
                connection = connection.execution_options(isolation_level="REPEATABLE READ")
            with connection.begin():
                if engine.dialect.name == "postgresql":
                    connection.exec_driver_sql("SET TRANSACTION READ ONLY")
                elif engine.dialect.name == "sqlite":
                    connection.exec_driver_sql("BEGIN")
                bindings = current_bindings(connection)
                version_ids = [b["source_version_id"] for b in bindings]
                source_rows = list(connection.execute(sa.select(models.sources)).mappings())
                for source in source_rows:
                    if permissions.required_for(source):
                        decisions = [permissions.decision(connection, source["key"], op)
                                     for op in ("display_internal", "display_public")]
                        has_version = any(b["source_key"] == source["key"] for b in bindings)
                        if has_version and not permissions.decision(connection, source["key"], "store")["allowed"]:
                            raise PermissionError(f"No permitted release storage for {source['key']}")
                        if has_version and not any(d["allowed"] for d in decisions):
                            raise PermissionError(f"No permitted release audience for {source['key']}")
                with target.begin() as output:
                    models.metadata.create_all(output)
                    counts = {}
                    for name in TABLE_NAMES:
                        table = getattr(models, name)
                        query = sa.select(*[
                            sa.cast(sa.null(), column.type).label(column.name)
                            if column.name.startswith("embedding") or column.name == "embedded_at" else column
                            for column in table.c
                        ])
                        if name == "source_versions":
                            query = query.where(table.c.id.in_(version_ids))
                        elif "source_version_id" in table.c:
                            query = query.where(table.c.source_version_id.in_(version_ids))
                        elif "clause_id" in table.c:
                            clause_ids = sa.select(models.clauses.c.id).where(models.clauses.c.source_version_id.in_(version_ids))
                            query = query.where(table.c.clause_id.in_(clause_ids))
                        elif "from_clause_id" in table.c:
                            clause_ids = sa.select(models.clauses.c.id).where(models.clauses.c.source_version_id.in_(version_ids))
                            query = query.where(table.c.from_clause_id.in_(clause_ids))
                        rows = [dict(r) for r in connection.execute(query).mappings()]
                        if rows:
                            output.execute(table.insert(), rows)
                        counts[name] = len(rows)
        destination.chmod(0o600)
        return {"bindings": bindings, "counts": counts, "audience": "restricted-reviewers",
                "table_allowlist": list(TABLE_NAMES)}
    except Exception:
        target.dispose()
        destination.unlink(missing_ok=True)
        raise
    finally:
        target.dispose()


def verify_snapshot_bindings(path: Path, expected: list[dict]) -> bool:
    path = Path(path)
    if not path.is_file():
        return False
    target = make_engine(f"sqlite:///{path.resolve().as_uri()}?mode=ro&uri=true")
    try:
        with target.connect() as conn:
            return current_bindings(conn) == expected
    finally:
        target.dispose()

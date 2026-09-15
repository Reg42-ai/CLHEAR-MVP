"""0026 — durable L1 jobs, leased source tasks and measured step evidence."""
from sqlalchemy.engine import Connection
from app.clhear.l1.workflow import jobs, tasks, steps, deliveries, source_leases


def upgrade(conn: Connection) -> None:
    for table in (jobs, tasks, steps, deliveries, source_leases):
        table.create(conn, checkfirst=True)

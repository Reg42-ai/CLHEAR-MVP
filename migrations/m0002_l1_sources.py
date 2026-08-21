"""0002 — l1_sources schema: families, sources, versions, doc_nodes (typed raw
document tree), clauses (provision projection), citations, discovery
candidates, change events, licenses, BYOL uploads.

HLD §6.2. The `clauses_public` / `nodes_public` discipline is enforced in
code via app.clhear.l1.public (the Aurora view + reader-role grants land
when reg42-infra is wired — # ARCH).
"""
from sqlalchemy.engine import Connection

from app.clhear.l1.models import ALL_TABLES


def upgrade(conn: Connection) -> None:
    for table in ALL_TABLES:
        table.create(conn, checkfirst=True)

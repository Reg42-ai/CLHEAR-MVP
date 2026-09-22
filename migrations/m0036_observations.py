"""0036 — observation documents (app / agent / process performance)."""
from sqlalchemy.engine import Connection

from app.clhear.observations import observations


def upgrade(conn: Connection) -> None:
    observations.create(conn, checkfirst=True)

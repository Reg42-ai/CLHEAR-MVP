"""0038 — one row per layer build of a scoped corpus: its inputs and output revision."""
from sqlalchemy.engine import Connection

from app.clhear.layer_builds import layer_builds


def upgrade(conn: Connection) -> None:
    layer_builds.create(conn, checkfirst=True)

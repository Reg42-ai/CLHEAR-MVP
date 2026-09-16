"""Persist worker-owned resumable publisher discovery frontiers."""
from app.clhear.l1.discovery import metadata


def upgrade(conn):
    metadata.create_all(conn)

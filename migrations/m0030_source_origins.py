"""Preserve source history while recording explicit test-origin exclusions."""


def upgrade(conn):
    from app.clhear.l1.origin import metadata
    metadata.create_all(conn)

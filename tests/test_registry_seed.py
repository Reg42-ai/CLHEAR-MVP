"""eToro blueprint registry seed: idempotency, integrity, API visibility."""
import sqlalchemy as sa

from scripts import seed_registry
from app.clhear.l1.models import family_members, source_families, sources


def _seed(engine):
    from app.clhear import db as dbmod

    dbmod._engine = engine
    seed_registry.main()


def test_seed_is_idempotent_and_complete(engine):
    _seed(engine)
    with engine.connect() as conn:
        n_fam = conn.execute(sa.select(sa.func.count()).select_from(source_families)).scalar()
        n_src = conn.execute(sa.select(sa.func.count()).select_from(sources)).scalar()
        n_mem = conn.execute(sa.select(sa.func.count()).select_from(family_members)).scalar()
    assert n_fam >= 20
    assert n_src >= 90
    assert n_mem == n_src  # every seeded source belongs to its family
    _seed(engine)  # second run adds nothing
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(sources)).scalar() == n_src


def test_every_family_has_exactly_one_root(engine):
    _seed(engine)
    with engine.connect() as conn:
        rows = conn.execute(
            sa.select(family_members.c.family_id, sa.func.count())
            .where(family_members.c.relation == "root")
            .group_by(family_members.c.family_id)
        ).all()
        n_fam = conn.execute(sa.select(sa.func.count()).select_from(source_families)).scalar()
    assert len(rows) == n_fam
    assert all(count == 1 for _, count in rows)


def test_blueprint_visible_in_library_api(engine, client):
    _seed(engine)
    data = client.get("/api/clhear/sources").json()
    by_key = {f["key"]: f for f in data}
    assert "eu-mifid" in by_key
    mifid = next(m for m in by_key["eu-mifid"]["members"] if m["key"] == "celex/32014L0065")
    assert mifid["short_name"] == "MiFID II"
    assert mifid["latest_version"] is None
    assert mifid["added_via"] == "watchlist"
    # restricted standards carry the restricted license flag
    standards = by_key["standards"]["members"]
    iso = next(m for m in standards if m["key"] == "iso/27001-2022")
    assert iso["license"] == "restricted"

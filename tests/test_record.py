"""L0 record discipline (HLD v2 I2, I3, I1): bi-temporal writes, why-trails,
layer-order guard, no deletion anywhere."""
import ast
import re
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.clhear.derived_models import concept_members, concepts
from app.clhear.platform import record
from app.clhear.platform.shared_schema import SHARED_COLUMN_NAMES

REPO = Path(__file__).resolve().parents[1]


def _seed_concept(conn):
    conn.execute(concepts.insert().values(id="CON:test", name="Test", canonical_statement="x"))


def test_write_requires_why_trail(engine):
    with engine.begin() as conn:
        _seed_concept(conn)
        with pytest.raises(record.WhyTrailRequired):
            record.write(conn, concept_members, {"concept_id": "CON:test", "obligation_id": "OBL-000001"}, why=None)


def test_write_populates_shared_columns_and_why_trail(engine):
    why = record.WhyTrail(
        layer="L2", reasoning_summary="member of concept", input_layers=("L1",),
        inputs=("OBL-000001",), confidence=0.93, agent_id="l2.consolidate", subject_ref="CON:test",
    )
    with engine.begin() as conn:
        _seed_concept(conn)
        values = record.write(conn, concept_members, {"concept_id": "CON:test", "obligation_id": "OBL-000001"}, why=why)
        row = conn.execute(sa.select(concept_members)).mappings().one()
        # the migrations already wrote L4 ontology trails (m0012); look at ours only
        trail = conn.execute(sa.select(record.why_trails).where(record.why_trails.c.id == values["why_trail_id"])).mappings().one()
    assert values["why_trail_id"].startswith("WHY-")
    assert row["why_trail_id"] == trail["id"]
    assert row["version"] == 1 and row["valid_to"] is None
    assert row["derived_by"] == "l2.consolidate"
    assert row["confidence"] == pytest.approx(0.93)
    assert row["inputs_hash"] == trail["inputs_hash"] != ""
    assert trail["layer"] == "L2"


def test_invalidate_sets_valid_to(engine):
    why = record.WhyTrail(layer="L2", reasoning_summary="seed")
    with engine.begin() as conn:
        _seed_concept(conn)
        record.write(conn, concept_members, {"concept_id": "CON:test", "obligation_id": "OBL-000001"}, why=why)
        n = record.invalidate(
            conn, concept_members, concept_members.c.obligation_id == "OBL-000001",
            why=record.WhyTrail(layer="L2", reasoning_summary="superseded"), reason="superseded",
        )
        assert n == 1
        row = conn.execute(sa.select(concept_members)).mappings().one()
        assert row["valid_to"] is not None
        assert row["version"] == 2
        assert row["review"][-1]["event"] == "invalidated"
        # The row still exists (I2) but is no longer in force.
        assert conn.execute(sa.select(sa.func.count()).select_from(concept_members)).scalar_one() == 1
        live = conn.execute(sa.select(concept_members).where(record.in_force(concept_members))).all()
        assert live == []


def test_layer_input_guard():
    record.assert_layer_inputs("L3", ("L1", "L2"))
    with pytest.raises(record.LayerOrderViolation):
        record.assert_layer_inputs("L2", ("L3",))
    with pytest.raises(record.LayerOrderViolation):
        record.assert_layer_inputs("L4", ("L5",))
    with pytest.raises(record.LayerOrderViolation):
        record.assert_layer_inputs("L1", ("L1",))  # a layer may not read itself as input


def test_write_rejects_lower_layer_reading_higher(engine):
    why = record.WhyTrail(layer="L2", reasoning_summary="bad", input_layers=("L6",))
    with engine.begin() as conn:
        _seed_concept(conn)
        with pytest.raises(record.LayerOrderViolation):
            record.write(conn, concept_members, {"concept_id": "CON:test", "obligation_id": "OBL-000002"}, why=why)


def test_rebuild_projection_only_for_projection_tables(engine):
    with engine.begin() as conn:
        with pytest.raises(record.DeletionForbidden):
            record.rebuild_projection(conn, concept_members, sa.true())


def test_all_tables_have_shared_columns(engine):
    missing = {}
    for table in record.layer_tables():
        cols = {c.name for c in table.columns}
        gap = set(SHARED_COLUMN_NAMES) - cols
        if gap:
            missing[table.name] = sorted(gap)
    assert missing == {}
    # and they exist physically after migrations (SQLite ALTER TABLE path)
    insp = sa.inspect(engine)
    for table in record.layer_tables():
        physical = {c["name"] for c in insp.get_columns(table.name)}
        assert set(SHARED_COLUMN_NAMES) <= physical, table.name


def test_no_delete_anywhere():
    """I2: record history is retained; only disposable projections may clear data."""
    offenders = []
    pattern = re.compile(r"\.delete\(\)|\bDELETE\s+FROM\b", re.IGNORECASE)
    for path in (REPO / "app").rglob("*.py"):
        if path.name == "record.py" and path.parent.name == "platform":
            continue
        source = path.read_text(encoding="utf-8")
        disposable_lines = set()
        if path.relative_to(REPO).as_posix() == "app/clhear/l1/viewer_snapshot.py":
            # This helper rejects any pre-existing schema or non-SQLite engine
            # before creating its disposable projection. Its guard and source
            # preservation are exercised in test_l1_viewer_snapshot.py.
            helper = next(node for node in ast.parse(source).body
                          if isinstance(node, ast.FunctionDef) and node.name == "_empty_schema")
            disposable_lines = set(range(helper.lineno, helper.end_lineno + 1))
        for lineno, line in enumerate(source.splitlines(), 1):
            if lineno in disposable_lines:
                continue
            if pattern.search(line):
                offenders.append(f"{path.relative_to(REPO)}:{lineno}: {line.strip()}")
    assert offenders == []


def test_needs_human_thresholds():
    assert record.needs_human("L2", None)
    assert record.needs_human("L2", 0.5)
    assert not record.needs_human("L2", 0.99)

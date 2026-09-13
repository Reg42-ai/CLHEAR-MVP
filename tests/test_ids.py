"""HLD v2 I11: stable ids — CLHEAR-<layer>.<n> for requirements, PREFIX-<n> for objects."""
import pytest

from app.clhear.platform import ids


def test_format_and_parse():
    assert ids.format_id("OBL", 7) == "OBL-000007"
    assert ids.is_object_id("OBL-000007")
    assert ids.is_object_id("BLK-123456789")
    assert not ids.is_object_id("OBL-7")
    assert not ids.is_object_id("XXX-000001")
    assert ids.layer_of("PRF-000001") == "L4"
    assert ids.layer_of("FIL-000001") == "L8"
    assert ids.layer_of("nope") is None
    with pytest.raises(ValueError):
        ids.format_id("ZZZ", 1)


def test_requirement_ids():
    assert ids.is_requirement_id("CLHEAR-2.14")
    assert ids.is_requirement_id("CLHEAR-12.3")
    assert not ids.is_requirement_id("CLHEAR-2")
    assert not ids.is_requirement_id("OBL-000001")


def test_every_hld_prefix_is_registered():
    for prefix, layer in {
        "OBL": "L2", "BLK": "L3", "PRF": "L4", "ACT": "L5", "BLU": "L6", "RSK": "L7", "FIL": "L8",
    }.items():
        assert ids.OBJECT_PREFIXES[prefix] == layer


def test_next_id_is_monotonic_per_prefix(engine):
    with engine.begin() as conn:
        a = ids.next_id(conn, "OBL")
        b = ids.next_id(conn, "OBL")
        c = ids.next_id(conn, "BLK")
        batch = ids.next_ids(conn, "OBL", 3)
    assert (a, b, c) == ("OBL-000001", "OBL-000002", "BLK-000001")
    assert batch == ["OBL-000003", "OBL-000004", "OBL-000005"]
    with engine.begin() as conn:
        assert ids.next_id(conn, "OBL") == "OBL-000006"  # persisted across transactions


def test_exposed_id_round_trip():
    assert ids.exposed_id("SRC", 42) == "SRC-000042"
    assert ids.integer_pk("SRC-000042") == 42
    with pytest.raises(ValueError):
        ids.integer_pk("42")

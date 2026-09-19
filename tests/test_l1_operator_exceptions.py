"""Private operator decisions never become publisher permission or acceptance."""
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.clhear.l1 import operator_exceptions as oe, permissions

KEY = "finra/rule/2210"
URL = "https://www.finra.org/rules-guidance/rulebooks/finra-rules/2210"


@pytest.fixture
def engine():
    value = sa.create_engine("sqlite://", execution_options={"schema_translate_map": {"l1_sources": None}})
    from migrations.m0034_l1_operator_exceptions import upgrade
    with value.begin() as conn:
        permissions.source_permissions.create(conn)
        upgrade(conn)
        upgrade(conn)
    yield value
    value.dispose()


def activate(engine, **overrides):
    values = dict(command_id="owner-activation", action="activate", approved_by="owner@example.test",
                  evidence_ref="review:actual-owner-exception", rationale="Private review; publisher permission remains unresolved")
    values.update(overrides)
    return oe.record_exception(engine, **values)


def bind(engine, active=None, **overrides):
    values = dict(activation_id=(active or activate(engine))["id"], manifest_hash="a" * 64,
                  scope_version="finra-reviewed-scope-v1", bound_by="l0.reviewed_manifest",
                  source_key=KEY, canonical_url=URL, source_role="document")
    values.update(overrides)
    return oe.bind_source(engine, **values)


def decision(engine, operation="parse", **kwargs):
    with engine.connect() as conn:
        return oe.decision(conn, KEY, operation, **kwargs)


def test_additive_migration_grants_nothing_and_missing_schema_fails_closed(engine):
    with engine.connect() as conn:
        assert oe.control_state(conn)["bindings"] == []
        assert not oe.latest_active(conn)
        for table in oe.TABLES:
            assert conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one() == 0
    assert not decision(engine)["allowed"]
    missing = sa.create_engine("sqlite://")
    with missing.connect() as conn:
        assert oe.decision(conn, KEY, "parse")["reason"] == "operator_exception_migration_required"
        assert oe.control_state(conn)["status"] == "unavailable"
    missing.dispose()


def test_activation_requires_exact_binding_and_only_allows_private_operations(engine):
    active = activate(engine)
    assert not decision(engine)["allowed"]
    bound = bind(engine, active)
    for operation in permissions.OPERATIONS:
        result = decision(engine, operation)
        assert result["allowed"] is (operation in oe.OPERATIONS)
        assert result["authority_type"] == "operator_exception" and not result["release_eligible"]
    result = decision(engine)
    assert result["exception_id"] == oe.FINRA_EXCEPTION_ID
    assert result["activation_id"] == active["id"] and result["binding_hash"] == bound["binding_hash"]
    assert result["permission_id"] == f"operator-exception:{active['id']}:{bound['id']}"
    assert result["expires_at"] is None and result["expiry_policy"] == "until_revoked"
    assert decision(engine, now=datetime(2100, 1, 1, tzinfo=timezone.utc))["allowed"]
    with engine.connect() as conn:
        assert not oe.decision(conn, KEY + "/a", "parse")["allowed"]
        assert not oe.decision(conn, "finra/rule/3110", "parse")["allowed"]
        assert not oe.decision(conn, "iso/27001-2022", "parse")["allowed"]


def test_idempotent_activation_revocation_and_old_command_never_reactivates(engine):
    first = activate(engine)
    first_binding = bind(engine, first)
    assert activate(engine) == first and bind(engine, first) == first_binding
    revoke = activate(engine, command_id="owner-revocation", action="revoke")
    assert activate(engine, command_id="owner-revocation", action="revoke") == revoke
    assert activate(engine) == first  # Bootstrap redelivery is not renewed authority.
    with engine.connect() as conn:
        assert oe.latest_active(conn) is None
        assert oe.latest_event(conn)["id"] == revoke["id"]
        assert conn.execute(sa.select(sa.func.count()).select_from(oe.exception_events)).scalar_one() == 2
    assert not decision(engine)["allowed"]
    with pytest.raises(ValueError, match="current active"):
        bind(engine, first, manifest_hash="b" * 64)
    again = activate(engine, command_id="new-owner-activation")
    assert again["id"] != first["id"] and not decision(engine)["allowed"]
    bind(engine, again)
    assert decision(engine)["activation_id"] == again["id"]


@pytest.mark.parametrize("changes", [
    {"exception_id": "iso-private-review"}, {"approved_by": ""}, {"evidence_ref": " "},
    {"rationale": ""}, {"command_id": ""}, {"action": "approve-license"},
])
def test_invalid_owner_record_does_not_write(engine, changes):
    with pytest.raises(ValueError):
        activate(engine, **changes)
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(oe.exception_events)).scalar_one() == 0


def test_reused_command_cannot_change_authority_or_claim(engine):
    first = activate(engine)
    with pytest.raises(ValueError, match="different exception"):
        activate(engine, action="revoke")
    with engine.connect() as conn:
        assert oe.latest_active(conn)["id"] == first["id"]


@pytest.mark.parametrize("changes", [
    {"source_key": "finra/*"}, {"source_key": "iso/rule/2210"},
    {"canonical_url": URL.replace("www.finra.org", "mirror.example.test")},
    {"canonical_url": URL.replace("https:", "http:")},
    {"canonical_url": URL.replace("www.finra.org", "attacker@www.finra.org")},
    {"canonical_url": URL + "/../3110"}, {"canonical_url": URL.replace("2210", "3110")},
    {"canonical_url": URL.replace("www.finra.org", "www.finra.org:8443")},
    {"canonical_url": URL + "?query=anything"}, {"canonical_url": "https://www.finra.org/about"},
    {"source_role": "collection"}, {"source_role": "any"}, {"manifest_hash": "not-a-sha"},
    {"scope_version": ""}, {"bound_by": ""}, {"activation_id": True},
])
def test_binding_rejects_unreviewed_identity_and_scope(engine, changes):
    active = activate(engine)
    with pytest.raises(ValueError):
        bind(engine, active, **changes)
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(oe.source_bindings)).scalar_one() == 0


def test_catalog_pagination_and_attachment_require_their_own_exact_bindings(engine):
    from app.clhear.l1.inventory import FINRA_CATEGORIES, _source_key
    category, _, base = FINRA_CATEGORIES[0]
    active = activate(engine)
    catalog = bind(engine, active, source_key=f"finra/catalog/{category}", canonical_url=base, source_role="collection")
    with engine.connect() as conn:
        assert oe.decision(conn, catalog["source_key"], "acquire", canonical_url=base)["allowed"]
        assert not oe.decision(conn, catalog["source_key"], "acquire", canonical_url=base + "?page=1")["allowed"]
        assert not oe.decision(conn, KEY, "acquire", canonical_url=URL)["allowed"]
    bind(engine, active, source_key=catalog["source_key"], canonical_url=base + "?page=1", source_role="collection")
    attachment = "https://files.finra.org/notice-2026-01.pdf"
    bind(engine, active, source_key=_source_key(attachment), canonical_url=attachment)
    with engine.connect() as conn:
        assert oe.decision(conn, catalog["source_key"], "acquire", canonical_url=base + "?page=1")["allowed"]
        assert oe.decision(conn, _source_key(attachment), "acquire", canonical_url=attachment)["allowed"]
    bind(engine, active, source_key="finra/rulebook", canonical_url="https://finra.org/rules-guidance/rulebooks/finra-rules", source_role="collection")


def test_retired_catalog_seed_keeps_its_ledger_binding_valid(engine):
    """finra.org retired /rulebooks/nasd-rules. The category stays declared but
    unseeded so the 16 Sep collection binding still validates; an invalid
    binding would make control_state unavailable and stop L0 publishing."""
    from app.clhear.l1.inventory import FINRA_CATEGORIES, finra_seed_categories
    declared = {key: url for key, _, url in FINRA_CATEGORIES}
    assert "nasd_archive" in declared and "nasd_archive" not in {k for k, _, _ in finra_seed_categories()}
    active = activate(engine)
    bind(engine, active, source_key="finra/catalog/nasd_archive", canonical_url=declared["nasd_archive"], source_role="collection")
    with engine.connect() as conn:
        state = oe.control_state(conn)
    assert state["status"] == "available" and len(state["bindings"]) == 1


def test_manifest_binding_idempotency_and_current_control_digest(engine):
    active = activate(engine)
    bound = bind(engine, active)
    with engine.connect() as conn:
        before = oe.control_state(conn)
        assert before == oe.control_state(conn)
        assert before["bindings"][0]["binding_id"] == bound["id"]
        assert "recorded_at" not in str(before)
    assert bind(engine, active) == bound
    with pytest.raises(ValueError, match="different manifest"):
        bind(engine, active, scope_version="changed-without-new-manifest")
    bind(engine, active, manifest_hash="b" * 64)
    with engine.connect() as conn:
        changed = oe.control_state(conn)
        assert changed["digest"] != before["digest"] and len(changed["bindings"]) == 2
    activate(engine, command_id="revoke", action="revoke")
    with engine.connect() as conn:
        revoked = oe.control_state(conn)
        assert revoked["bindings"] == [] and revoked["digest"] != changed["digest"]
        assert revoked["exceptions"][0]["action"] == "revoke"


def test_invalid_persisted_evidence_fails_closed(engine):
    bind(engine)
    with engine.begin() as conn:
        conn.execute(oe.source_bindings.update().values(canonical_url=URL.replace("2210", "3110")))
    assert not decision(engine)["allowed"]
    with engine.connect() as conn:
        assert oe.control_state(conn)["status"] == "unavailable"
    with engine.begin() as conn:
        conn.execute(oe.exception_events.update().values(rationale="edited after approval"))
    assert not decision(engine)["allowed"]
    with engine.connect() as conn:
        assert oe.control_state(conn)["reason"] == "invalid_exception_evidence"


def test_candidate_helper_preserves_strict_publisher_permissions_and_grant_priority(engine):
    bind(engine)
    with engine.connect() as conn:
        before = permissions.decision(conn, KEY, "parse")
        candidate = permissions.candidate_decision(conn, KEY, "parse", canonical_url=URL)
        assert candidate["allowed"] and not candidate["publisher_permission"]["allowed"]
        assert before == permissions.decision(conn, KEY, "parse")
        assert conn.execute(sa.select(sa.func.count()).select_from(permissions.source_permissions)).scalar_one() == 0
        for forbidden in set(permissions.OPERATIONS) - oe.OPERATIONS:
            assert not permissions.candidate_decision(conn, KEY, forbidden)["allowed"]
    grant = permissions.record_permission(engine, source_key=KEY, permissions={"parse": True},
        evidence_ref="contract:real-publisher-permission", approved_by="rights-owner", approved=True)
    with engine.connect() as conn:
        result = permissions.candidate_decision(conn, KEY, "parse")
        assert result["allowed"] and result["permission_id"] == grant["id"]
        assert result["authority_type"] == "publisher_permission" and result["release_eligible"]


def test_reader_key_reuse_cannot_authorize_changed_canonical_url(engine):
    bind(engine)
    with engine.begin() as conn:
        conn.exec_driver_sql("CREATE TABLE sources (key TEXT PRIMARY KEY, canonical_url TEXT)")
        conn.exec_driver_sql("INSERT INTO sources (key, canonical_url) VALUES (?, ?)", (KEY, URL))
    assert decision(engine)["allowed"]
    with engine.begin() as conn:
        conn.exec_driver_sql("UPDATE sources SET canonical_url = ?", (URL.replace("2210", "3110"),))
    assert not decision(engine)["allowed"]


def test_old_snapshot_ledgers_reproduce_control_but_revocation_changes_current_state(engine):
    bind(engine)
    copy_engine = sa.create_engine("sqlite://", execution_options={"schema_translate_map": {"l1_sources": None}})
    with engine.connect() as source, copy_engine.begin() as target:
        for table in oe.TABLES:
            table.create(target)
            rows = [dict(row) for row in source.execute(sa.select(table)).mappings()]
            if rows:
                target.execute(table.insert(), rows)
        assert oe.control_state(source) == oe.control_state(target)
        snapshot_digest = oe.control_state(target)["digest"]
    activate(engine, command_id="revoke", action="revoke")
    with engine.connect() as current, copy_engine.connect() as old:
        assert oe.control_state(current)["digest"] != snapshot_digest
        assert oe.control_state(old)["digest"] == snapshot_digest
    copy_engine.dispose()

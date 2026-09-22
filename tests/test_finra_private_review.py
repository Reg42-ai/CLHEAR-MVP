"""Owner-bound FINRA discovery uses L0 approvals and the ordinary L1 path."""
import uuid
from datetime import datetime, timezone

import pytest
import sqlalchemy as sa

from app.clhear import workers
from app.clhear.l1 import discovery, finra_private_review as review, inventory, operator_exceptions as exceptions, permissions
from app.clhear.l1.pipeline import LocalStore
from app.clhear.models import events
from app.clhear.platform.events import Envelope

INDEX = "https://www.finra.org/rules-guidance/rulebooks/finra-rules"
NEW_RULE = INDEX + "/9999"


def bootstrap(engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "a" * 40)
    return review.bootstrap(engine)


def revoke(engine):
    return exceptions.record_exception(engine, exception_id=exceptions.FINRA_EXCEPTION_ID,
        command_id="test-revocation", action="revoke", approved_by="owner", evidence_ref="test:revoke",
        rationale="End the private review")


def test_bootstrap_idempotent_and_never_reactivates_revocation(engine, monkeypatch):
    first = bootstrap(engine, monkeypatch)
    assert first["status"] == "active" and first["release_eligible"] is False
    assert review.bootstrap(engine) == first
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(exceptions.exception_events)).scalar_one() == 1
        assert not permissions.decision(conn, "finra/rule/2210", "acquire")["allowed"]
        assert permissions.candidate_decision(conn, "finra/rule/2210", "acquire")["allowed"]
    revoke(engine)
    monkeypatch.setenv("CLHEAR_CODE_REVISION", "b" * 40)
    assert review.bootstrap(engine) == {"status": "revoked", "reactivated": False}
    with engine.connect() as conn:
        assert not permissions.candidate_decision(conn, "finra/rule/2210", "acquire")["allowed"]


def test_only_l0_can_bind_the_owner_manifest(engine, monkeypatch):
    monkeypatch.setenv("CLHEAR_FLEET", "l1")
    with pytest.raises(PermissionError):
        review.bootstrap(engine)


def test_new_frontier_waits_for_l0_then_resumes_and_retains_exact_evidence(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(inventory, "FINRA_CATEGORIES", (("rules", "Rules", INDEX),))
    bootstrap(engine, monkeypatch)
    fetched = []
    def fetch(url):
        fetched.append(url)
        return ((f'<main><a href="{NEW_RULE}">Rule</a></main>' if url == INDEX else '<main><h1>Test-only rule</h1></main>').encode(), "live")
    monkeypatch.setattr(inventory, "_fetch_discovery", fetch)
    monkeypatch.setenv("CLHEAR_FLEET", "l1")
    _, first = inventory._discover(engine, LocalStore(tmp_path))
    assert fetched == [INDEX]
    assert first["pending_pages"] == 1
    with engine.connect() as conn:
        command = conn.execute(sa.select(events).where(events.c.kind == "L1ExceptionBindingsRequested")).mappings().one()
        assert not permissions.candidate_decision(conn, "finra/rule/9999", "acquire")["allowed"]
    envelope = Envelope(event_id=str(command["id"]), layer="l0", kind=command["kind"],
        subject_ref=command["subject_ref"], payload=command["payload"], producer="l1.discovery",
        ts=datetime.now(timezone.utc).isoformat())
    with pytest.raises(workers.WrongFleet):
        workers.handle_envelope(engine, None, envelope.model_dump_json())
    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    result = workers.handle_envelope(engine, None, envelope.model_dump_json())
    assert result["bound"] == 1 and result["blocked"] == 0
    assert workers.handle_envelope(engine, None, envelope.model_dump_json()) is None
    monkeypatch.setenv("CLHEAR_FLEET", "l1")
    entries, second = inventory._discover(engine, LocalStore(tmp_path))
    # A FINRA Rule is enumerated from its index and bound, but discovery never
    # fetches it: finra.org's request budget is spent once, by the import.
    assert fetched == [INDEX]
    assert second["pending_pages"] == 0 and "finra/rule/9999" in entries
    with engine.connect() as conn:
        page = conn.execute(sa.select(discovery.pages).where(discovery.pages.c.source_key == "finra/rule/9999")).mappings().one()
        assert page["status"] == "checked" and page["result"]["terminal"] is True and page["attempts"] == 0
        decision = permissions.candidate_decision(conn, "finra/rule/9999", "acquire", canonical_url=NEW_RULE)
        assert decision["allowed"] and decision["authority_type"] == "operator_exception" and decision["release_eligible"] is False


def test_revoke_while_frontier_waits_leaves_a_gap_not_an_endless_pending_page(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(inventory, "FINRA_CATEGORIES", (("rules", "Rules", INDEX),))
    bootstrap(engine, monkeypatch)
    monkeypatch.setattr(inventory, "_fetch_discovery", lambda url: (f'<main><a href="{NEW_RULE}">Rule</a></main>'.encode(), "live"))
    _, report = inventory._discover(engine, LocalStore(tmp_path))
    revoke(engine)
    result = review.bind_frontier(engine, report["cycle_id"])
    assert result["bound"] == 0 and result["blocked"] == 1
    assert discovery.read_cycle(engine, report["cycle_id"])[1]["pending_pages"] == 0


def test_discovery_manifest_change_cannot_gain_new_bindings(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(inventory, "FINRA_CATEGORIES", (("rules", "Rules", INDEX),))
    bootstrap(engine, monkeypatch)
    monkeypatch.setattr(inventory, "_fetch_discovery", lambda url: (f'<main><a href="{NEW_RULE}">Rule</a></main>'.encode(), "live"))
    _, report = inventory._discover(engine, LocalStore(tmp_path))
    monkeypatch.setattr(inventory, "SCOPE_VERSION", "different-unreviewed-version")
    with pytest.raises(ValueError, match="reviewed FINRA manifest"):
        review.bind_frontier(engine, report["cycle_id"])


def test_operator_review_invalidates_before_commit_and_republishes_with_token(engine, monkeypatch):
    from app.clhear.l1 import operator_access
    bootstrap(engine, monkeypatch)
    observed = []
    def invalidate(*, mutation_id):
        assert mutation_id
        with engine.connect() as conn:
            assert exceptions.latest_active(conn) is not None
        observed.append("invalidate")
        return {"invalidation_token": "test-token"}
    def publish(engine, *, invalidation_token):
        assert invalidation_token == "test-token"
        with engine.connect() as conn:
            assert exceptions.latest_active(conn) is None
        observed.append("publish")
    monkeypatch.setattr(operator_access, "invalidate_configured_control", invalidate)
    monkeypatch.setattr(operator_access, "publish_configured_control", publish)
    envelope = Envelope(event_id=str(uuid.uuid4()), layer="l0", kind="L1EvidenceReviewRecorded", subject_ref="finra",
        payload={"review_kind": "operator_exception", "exception_id": exceptions.FINRA_EXCEPTION_ID,
                 "command_id": "review-revoke", "action": "revoke", "approved_by": "owner",
                 "evidence_ref": "test:owner-review", "rationale": "Revocation"},
        producer="test-owner", ts=datetime.now(timezone.utc).isoformat())
    assert workers.handle_envelope(engine, None, envelope.model_dump_json())["review_kind"] == "operator_exception"
    assert observed == ["invalidate", "publish"]


def test_rejected_frontier_identity_is_terminal_for_its_activation_and_manifest(engine, monkeypatch):
    bootstrap(engine, monkeypatch)
    contract = review.manifest()
    with engine.begin() as conn:
        conn.execute(discovery.cycles.insert().values(id="rejected", publisher_id="finra",
            profile_hash=contract["profile_hash"], cycle_date="2026-09-16"))
        conn.execute(discovery.pages.insert().values(**discovery._page("rejected", NEW_RULE,
            "finra/rule/8888", "rules", "document")))
    review.request_frontier_bindings(engine, "rejected")
    assert review.bind_frontier(engine, "rejected")["blocked"] == 1
    review.request_frontier_bindings(engine, "rejected")
    assert discovery.read_cycle(engine, "rejected")[1]["pending_pages"] == 0
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(events).where(
            events.c.kind == "L1ExceptionBindingsRequested")).scalar_one() == 1


def test_original_import_and_repeat_keep_bytes_and_never_call_models(engine, monkeypatch, tmp_path):
    from app.clhear.l1 import pipeline
    from app.clhear.l1.models import source_versions
    from tests.test_l1_finra import adapter as finra_adapter
    from tests.test_l1_inventory import BODY
    from app.clhear.l1.adapters.base import Artifact, FetchResult
    bootstrap(engine, monkeypatch)
    adapter = finra_adapter()
    monkeypatch.setattr(adapter, "fetch", lambda *_: FetchResult(version_label="test-edition",
        artifacts=[Artifact("original.html", BODY, "text/html")], tree=adapter.parse(BODY)))
    store = LocalStore(tmp_path)
    first = pipeline.ingest(engine, adapter, store, gateway=object())
    assert first["status"] == "added", first
    assert (tmp_path / first["artifact_manifest"][0]["key"]).read_bytes() == BODY
    second = pipeline.ingest(engine, adapter, store, gateway=object())
    assert second["status"] == "unchanged", second
    assert first["source_version_id"] == second["source_version_id"]
    revoke(engine)
    monkeypatch.setattr(adapter, "fetch", lambda *_: pytest.fail("revoked exception fetched a document"))
    assert pipeline.ingest(engine, adapter, store)["status"] == "rights-blocked"
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(source_versions)).scalar_one() == 1


def test_revocation_during_artifact_write_cannot_commit_a_source_version(engine, monkeypatch, tmp_path):
    from app.clhear.l1 import pipeline
    from app.clhear.l1.models import source_versions
    from app.clhear.l1.adapters.base import Artifact, FetchResult
    from tests.test_l1_finra import adapter as finra_adapter
    from tests.test_l1_inventory import BODY
    bootstrap(engine, monkeypatch)
    adapter = finra_adapter()
    monkeypatch.setattr(adapter, "fetch", lambda *_: FetchResult(version_label="test-edition",
        artifacts=[Artifact("original.html", BODY, "text/html")], tree=adapter.parse(BODY)))
    class RevokingStore(LocalStore):
        def put(self, key, content, content_type):
            uri = super().put(key, content, content_type)
            revoke(engine)
            return uri
    result = pipeline.ingest(engine, adapter, RevokingStore(tmp_path))
    assert result["status"] == "failed", result
    assert "authorization changed while archiving" in result["error"]
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(source_versions)).scalar_one() == 0


def test_publisher_denial_is_retained_separately_and_never_overridden_by_the_exception(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(inventory, "FINRA_CATEGORIES", (("rules", "Rules", INDEX),))
    bootstrap(engine, monkeypatch)
    permissions.record_permission(engine, source_key="finra/rule/9999", permissions={"acquire": True, "store": True, "parse": True},
                                  evidence_ref="test:publisher-denial", approved_by="publisher", approved=False)
    monkeypatch.setattr(inventory, "_fetch_discovery", lambda url: (f'<main><a href="{NEW_RULE}">Rule</a></main>'.encode(), "live"))
    monkeypatch.setenv("CLHEAR_FLEET", "l1")
    _, report = inventory._discover(engine, LocalStore(tmp_path))
    with engine.connect() as conn:
        page = conn.execute(sa.select(discovery.pages).where(discovery.pages.c.source_key == "finra/rule/9999")).mappings().one()
        requests = conn.execute(sa.select(sa.func.count()).select_from(events).where(events.c.kind == "L1ExceptionBindingsRequested")).scalar_one()
        decision = permissions.candidate_decision(conn, "finra/rule/9999", "acquire", canonical_url=NEW_RULE)
    assert page["status"] == "permission_blocked" and page["result"]["publisher_denied"] is True
    assert any(f["code"] == "publisher_permission_denied" for f in page["result"]["findings"])
    assert requests == 0  # a denial is not a binding request
    assert not decision["allowed"] and decision["reason"] == "not_approved" and decision.get("denied") is True
    assert decision["authority_type"] == "publisher_permission"  # the exception was not consulted
    assert report["pending_pages"] == 0


def test_repeated_frontier_batches_request_one_binding_and_binding_twice_is_idempotent(engine, monkeypatch, tmp_path):
    monkeypatch.setattr(inventory, "FINRA_CATEGORIES", (("rules", "Rules", INDEX),))
    bootstrap(engine, monkeypatch)
    monkeypatch.setattr(inventory, "_fetch_discovery", lambda url: (f'<main><a href="{NEW_RULE}">Rule</a><a href="{INDEX}/9998">Rule</a></main>'.encode(), "live"))
    monkeypatch.setenv("CLHEAR_FLEET", "l1")
    _, report = inventory._discover(engine, LocalStore(tmp_path))
    review.request_frontier_bindings(engine, report["cycle_id"])  # a second batch before L0 relayed the first request
    review.request_frontier_bindings(engine, report["cycle_id"])
    with engine.connect() as conn:
        requests = conn.execute(sa.select(sa.func.count()).select_from(events).where(events.c.kind == "L1ExceptionBindingsRequested")).scalar_one()
        waiting = conn.execute(sa.select(sa.func.count()).select_from(discovery.pages).where(discovery.pages.c.status == "awaiting_exception_binding")).scalar_one()
    assert requests == 1 and waiting == 2
    monkeypatch.setenv("CLHEAR_FLEET", "l0")
    first = review.bind_frontier(engine, report["cycle_id"])
    second = review.bind_frontier(engine, report["cycle_id"])
    assert first["bound"] == 2 and second == {**first, "bound": 0, "blocked": 0}
    with engine.connect() as conn:
        bindings = conn.execute(sa.select(sa.func.count()).select_from(exceptions.source_bindings)
                                .where(exceptions.source_bindings.c.source_key.in_(["finra/rule/9999", "finra/rule/9998"]))).scalar_one()
        checked = conn.execute(sa.select(sa.func.count()).select_from(discovery.pages).where(
            discovery.pages.c.status == "checked", discovery.pages.c.role == "document")).scalar_one()
    assert bindings == 2 and checked == 2  # one binding per rule; enumerated leaves need no discovery fetch

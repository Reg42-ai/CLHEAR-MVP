"""P0 done-test (HLD §9): dummy-fleet rehearsal.

Event through outbox -> queue -> worker; one gateway call logged with cost;
proposal approved with identity; downstream event emitted; tag exports an
empty-but-valid snapshot with green (empty-skeleton) evals.
"""
import json

import sqlalchemy as sa

from app.clhear.models import events, llm_calls, proposals, runs
from app.clhear.platform.events import InMemoryTransport, relay_once
from app.clhear.platform.evals import run_all
from app.clhear.platform.exporter import export_release
from app.clhear.platform.gateway import FakeProvider, Gateway
from app.clhear.workers import handle_envelope, run_dummy_fleet

RELEASE = "clhear-v0.0.0-rehearsal"


def test_dummy_fleet_rehearsal(engine, client, tmp_path):
    # 1. Dummy fleet: data change + outbox event in one transaction.
    event_id = run_dummy_fleet(engine)
    with engine.connect() as conn:
        outbox_row = conn.execute(sa.select(events).where(events.c.event_id == event_id)).one()
    assert outbox_row.relayed_at is None
    assert outbox_row.kind == "DummyChanged"

    # 2. Relay ships it to the queue and stamps relayed_at.
    transport = InMemoryTransport()
    assert relay_once(engine, transport) == 1
    with engine.connect() as conn:
        assert conn.execute(sa.select(events.c.relayed_at).where(events.c.event_id == event_id)).scalar_one()

    # 3. Worker consumes; one gateway call logged with cost; proposal created.
    provider = FakeProvider()
    gateway = Gateway(engine, provider)
    body = transport.receive()
    assert body is not None
    outputs = handle_envelope(engine, gateway, body)
    assert provider.calls == 1
    with engine.connect() as conn:
        call_row = conn.execute(sa.select(llm_calls)).one()
        assert float(call_row.cost_usd) > 0
        assert call_row.fleet == "dummy"
        assert len(call_row.prompt_hash) == 64

    # Idempotency: redelivery of the same envelope is a no-op.
    transport.send(body)
    assert handle_envelope(engine, gateway, transport.receive()) is None
    assert provider.calls == 1

    # 4. Proposal approved via API with a recorded maintainer identity.
    proposal_id = outputs["proposal_id"]
    resp = client.post(
        f"/api/clhear/proposals/{proposal_id}/approve",
        headers={"X-Reg42-User": "avner@reg42.ai"},
    )
    assert resp.status_code == 200, resp.text
    decided = resp.json()
    assert decided["status"] == "approved"
    assert decided["approver"] == "avner@reg42.ai"
    assert decided["decided_at"]

    # ...and the downstream ProposalApproved event landed in the outbox.
    with engine.connect() as conn:
        downstream = conn.execute(sa.select(events).where(events.c.kind == "ProposalApproved")).one()
    payload = downstream.payload if isinstance(downstream.payload, dict) else json.loads(downstream.payload)
    assert payload["proposal_id"] == proposal_id
    assert payload["approver"] == "avner@reg42.ai"

    # 5. Green (skeleton) evals + tag exports an empty-but-valid snapshot.
    records = run_all(engine, release=RELEASE)
    assert records and all(r["passed"] for r in records)

    out_dir = tmp_path / "public-repo"
    result = export_release(engine, RELEASE, repo_dir=out_dir)
    snapshot = json.loads((out_dir / "snapshots" / RELEASE / "l1" / "snapshot.json").read_text())
    assert snapshot["release"] == RELEASE
    assert snapshot["sources"] == []          # empty-but-valid
    assert snapshot["all_evals_passed"] is True
    assert (out_dir / "evals" / f"{RELEASE}.json").exists()
    assert (out_dir / "snapshots" / RELEASE / "l1" / "snapshot.yaml").exists()
    assert result["snapshot"]["eval_scores"]

    # Everything was recorded in the run ledger (replayability).
    with engine.connect() as conn:
        fleets = {row.fleet for row in conn.execute(sa.select(runs.c.fleet))}
    assert {"dummy", "worker"} <= fleets

"""DR drills (HLD v2 §7.1 "Status page with SLOs; DR drills"; item 17): backup →
restore into a scratch target → verify record, graph and datalake replica → RPO/RTO
recorded, surfaced on the status page, and alarmed when missing."""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.clhear.derived_models import blocks as blocks_t
from app.clhear.l1 import pipeline
from app.clhear.models import events
from app.clhear.platform import dr, metrics, record
from tests.test_l1_synthetic_amendment import V1, SyntheticAdapter

ROOT = Path(__file__).resolve().parents[1]


def _seed(engine, tmp_path):
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01"), pipeline.LocalStore(tmp_path / "lake"))
    with engine.begin() as conn:
        record.write(conn, blocks_t, {"id": "BLK-DR-1", "name": "Drill block", "description": "", "capability": "", "evidence_artifacts": [],
                                      "satisfies": [], "implements_controls": [], "status": "curated", "kind": "Document", "purpose": "p"},
                     why=record.WhyTrail(layer="L3", reasoning_summary="dr seed", agent_id="agent:test"))


class FakeS3:
    """Enough of the S3 client for the replica check: a source bucket, a replica and a rule."""

    def __init__(self, *, rule_enabled=True, versioning="Enabled", missing=(), corrupt=()):
        self.src = {f"public-ok/l1/{i}.json": f'"etag-{i}"' for i in range(5)}
        self.rep = {k: (v if k not in corrupt else '"etag-x"') for k, v in self.src.items() if k not in missing}
        self.rule_enabled, self.versioning = rule_enabled, versioning

    def get_bucket_replication(self, Bucket):
        return {"ReplicationConfiguration": {"Rules": [{"Status": "Enabled" if self.rule_enabled else "Disabled",
                                                        "Destination": {"Bucket": "arn:aws:s3:::lake-replica"}}]}}

    def get_bucket_versioning(self, Bucket):
        return {"Status": self.versioning}

    def list_objects_v2(self, Bucket, MaxKeys):
        return {"Contents": [{"Key": k, "ETag": v} for k, v in list(self.src.items())[:MaxKeys]]}

    def head_object(self, Bucket, Key):
        if Key not in self.rep:
            raise KeyError(Key)
        return {"ETag": self.rep[Key]}


# --------------------------------------------------------------------------- the drill


def test_drill_restores_into_scratch_and_verifies_record_graph_and_replica(engine, tmp_path):
    _seed(engine, tmp_path)
    report = dr.run(engine, workdir=tmp_path / "dr", release="2026.09.13", datalake=("lake", "lake-replica"), s3_client=FakeS3(),
                    trigger="test")
    assert report["status"] == "passed", json.dumps(report, indent=2, default=str)
    rec, graph, lake = report["checks"]["record"], report["checks"]["graph"], report["checks"]["datalake"]
    assert rec["passed"] and not rec["mismatches"] and rec["migrations_equal"] and rec["rows_restored"] == rec["rows_source"] > 0
    assert rec["why_trails_checked"] > 0 and not rec["dangling_why_trails"]
    assert graph["passed"] and graph["checksum_live"] == graph["checksum_restored"] and graph["nodes"] > 0
    assert lake["passed"] and lake["sampled"] == 5 and lake["replication_rule_enabled"] and lake["replica_versioning"]
    # objectives measured, not asserted by fiat
    assert report["rpo_seconds"] is not None and report["rpo_seconds"] <= dr.TARGETS["rpo_seconds"]
    assert report["rto_seconds"] <= dr.TARGETS["rto_seconds"] and report["objectives"] == {"rpo_ok": True, "rto_ok": True}
    # the scratch target is a separate database, and the live record is untouched by the drill
    assert report["scratch_target"] != engine.url.render_as_string(hide_password=True)
    assert (tmp_path / "dr" / "restored.db").exists() and (tmp_path / "dr" / "record.db").exists()
    with engine.connect() as conn:
        assert conn.execute(sa.select(sa.func.count()).select_from(blocks_t).where(blocks_t.c.id == "BLK-DR-1")).scalar() == 1
    # recorded + published
    last = dr.last_drill(engine)
    assert last["drilled"] and last["passed"] and last["release"] == "2026.09.13"
    assert last["checks"] == {"record": True, "graph": True, "datalake": True}
    with engine.connect() as conn:
        ev = conn.execute(sa.select(events).where(events.c.kind == dr.EVENT_KIND)).mappings().all()
    assert len(ev) == 1 and ev[0]["payload"]["status"] == "passed"


def test_drill_refuses_the_live_record_as_scratch_target(engine, tmp_path):
    with pytest.raises(RuntimeError, match="must not be the live record"):
        dr.run(engine, scratch_url=engine.url.render_as_string(hide_password=False), workdir=tmp_path / "dr", skip_datalake=True)


def test_restore_refuses_to_overwrite_a_non_empty_scratch(engine, tmp_path):
    dump = tmp_path / "b.db"
    dr.backup(engine, dump)
    busy = tmp_path / "busy.db"
    busy.write_bytes(b"not empty")
    with pytest.raises(RuntimeError, match="not empty"):
        dr.restore(dump, f"sqlite:///{busy}")


def test_a_tampered_restore_fails_the_record_check_and_the_drill(engine, tmp_path, monkeypatch):
    _seed(engine, tmp_path)
    real_restore = dr.restore

    def tampered(dump, scratch_url):
        restored = real_restore(dump, scratch_url)
        with restored.begin() as conn:  # a restore that lost a clause and the why-trail behind a block
            conn.execute(sa.text("DELETE FROM clauses WHERE id = (SELECT MIN(id) FROM clauses)"))
            conn.execute(sa.text("DELETE FROM why_trails WHERE id = (SELECT why_trail_id FROM blocks WHERE id = 'BLK-DR-1')"))
        return restored

    monkeypatch.setattr(dr, "restore", tampered)
    report = dr.run(engine, workdir=tmp_path / "dr", skip_datalake=True, trigger="test")
    assert report["status"] == "failed"
    rec = report["checks"]["record"]
    assert not rec["passed"] and {m["table"] for m in rec["mismatches"]} == {"clauses", "why_trails"}
    assert rec["dangling_why_trails"]
    assert not report["checks"]["graph"]["passed"]  # the projection of a lossy restore cannot match
    assert dr.last_drill(engine)["passed"] is False


@pytest.mark.parametrize("bad, field", [
    (dict(rule_enabled=False), "replication_rule_enabled"),
    (dict(versioning="Suspended"), "replica_versioning"),
    (dict(missing=("public-ok/l1/2.json",)), "missing"),
    (dict(corrupt=("public-ok/l1/3.json",)), "etag_mismatch"),
])
def test_replica_check_catches_each_failure_mode(bad, field):
    out = dr.verify_datalake("lake", "lake-replica", client=FakeS3(**bad))
    assert out["passed"] is False
    assert out[field] in (False, ["public-ok/l1/2.json"], ["public-ok/l1/3.json"])


def test_replica_check_against_the_wrong_destination_fails():
    class Elsewhere(FakeS3):
        def get_bucket_replication(self, Bucket):
            return {"ReplicationConfiguration": {"Rules": [{"Status": "Enabled", "Destination": {"Bucket": "arn:aws:s3:::somewhere-else"}}]}}

    assert dr.verify_datalake("lake", "lake-replica", client=Elsewhere())["replication_targets_replica"] is False


# --------------------------------------------------------------------------- status page + metrics + schedule


def test_status_page_and_metrics_show_the_drill_and_age_it_out(engine, tmp_path, client):
    st = metrics.status(engine)
    slo = next(s for s in st["slos"] if s["name"] == "dr_drilled")
    assert st["dr"]["drilled"] is False and slo["met"] is False  # never drilled = not met, honestly
    _seed(engine, tmp_path)
    dr.run(engine, workdir=tmp_path / "dr", skip_datalake=True, trigger="test")
    st = metrics.status(engine)
    assert st["dr"]["drilled"] and next(s for s in st["slos"] if s["name"] == "dr_drilled")["met"] is True
    body = client.get("/status.json").json()
    assert body["dr"]["passed"] is True and body["dr"]["checks"]["record"] is True
    text = client.get("/metrics").text
    assert "clhear_dr_drill_passed 1" in text and "clhear_dr_rpo_seconds" in text and "clhear_dr_rto_seconds" in text
    # three days later without a drill: the SLO is missed even though the last run passed
    later = datetime.now(timezone.utc) + timedelta(days=3)
    st = metrics.status(engine, now=later)
    assert st["dr"]["drilled"] is False and next(s for s in st["slos"] if s["name"] == "dr_drilled")["met"] is False


def test_worker_handles_the_scheduled_drill_and_the_ledger_is_append_only(engine, tmp_path, monkeypatch):
    from app.clhear import workers
    from app.clhear.platform.events import Envelope

    _seed(engine, tmp_path)
    monkeypatch.setenv("CLHEAR_DR_SCRATCH_URL", f"sqlite:///{tmp_path}/scratch.db")
    assert "DrDrillRequested" in workers.HANDLERS and "DrDrillRequested" in workers._ALWAYS_RUN
    env = Envelope(event_id="schedule-dr-drill", layer="l0", kind="DrDrillRequested", subject_ref="all",
                   payload={"skip_datalake": True}, schema_version=1, producer="eventbridge", ts="")
    out = workers.handle_dr_drill(engine, None, env)
    assert out["status"] == "passed" and out["trigger"] == "event"
    assert len(dr.history(engine)) == 1
    src = (ROOT / "app/clhear/platform/dr.py").read_text()
    assert "dr_drills.update(" not in src and "dr_drills.delete(" not in src


def test_schedule_workflow_and_infra_exist_and_agree():
    wf = (ROOT / ".github/workflows/dr_drill.yml").read_text()
    assert "schedule:" in wf and "cron:" in wf and "pgvector/pgvector" in wf and "neo4j:" in wf
    assert "dr.run(" in wf and "CLHEAR_DATALAKE_REPLICA_BUCKET" in wf and "retention-days: 90" in wf
    eb = (ROOT / "infra/eventbridge.tf").read_text()
    assert 'resource "aws_cloudwatch_event_rule" "dr_drill"' in eb and '"DrDrillRequested"' in eb
    obs = (ROOT / "infra/observability.tf").read_text()
    assert 'metric_name         = "DrDrillPassed"' in obs and 'treat_missing_data  = "breaching"' in obs
    s3 = (ROOT / "infra/s3.tf").read_text()
    assert 'resource "aws_s3_bucket_replication_configuration" "datalake"' in s3 and "datalake_replica" in s3
    up = (ROOT / "status/.upptimerc.yml").read_text()
    assert "/status.json" in up and "*/5 * * * *" in up

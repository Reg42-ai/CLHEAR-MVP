"""Evidence APIs sanitize direct DB records even for a revoked private override."""
import json

import pytest
import sqlalchemy as sa

from app.clhear.l1 import workflow
from app.clhear.l1.pipeline import RunRecorder
from app.clhear.l1.models import source_versions
from app.clhear.models import runs, eval_runs, events
from tests.test_l1_operator_exceptions import activate, bind, KEY
from tests.test_l1_viewer_snapshot import corpus, MARKER
from tests.test_review_access import restricted_settings, restricted_client, _session


def recorded_failure(engine):
    version_id = corpus(engine)
    bind(engine)
    job = "private-error-evidence"
    workflow.ensure_job(engine, job, "finra", "manual", "event-evidence")
    task = workflow.ensure_task(engine, job, KEY)
    token = workflow.claim_task(engine, task)
    error = RuntimeError("Malformed publisher document: " + MARKER)
    with workflow.bind_execution(engine, job, task, token):
        with pytest.raises(RuntimeError):
            with workflow.stage("acquisition_parse", {"source_key": KEY, "error_type": "RuntimeError"}):
                raise error
    workflow.finish_task(engine, task, token, status="failed", error=error,
                         summary={"source_version_id": version_id, "error": str(error),
                                  "error_type": "RuntimeError", "coverage": 0.5,
                                  "violations": [MARKER], "nested": {"raw_text": MARKER}})
    workflow.update_job(engine, job, "failed", summary={"error": str(error), "source_keys": [KEY], "failed_sources": 1})
    with engine.connect() as conn:
        version = conn.execute(sa.select(source_versions).where(source_versions.c.id == version_id)).mappings().one()
    run = RunRecorder(engine, "l1.finra", "manual", {"source": KEY, "job_id": job, "prompt": MARKER})
    run.stage("fetch", error=str(error), error_type="RuntimeError", violations=[MARKER], duration_ms=123)
    run.finish("failed", {"source": KEY, "version": version["version_label"], "content_hash": version["content_hash"],
                          "source_version_id": version_id, "error": str(error), "error_type": "RuntimeError",
                          "coverage": 0.5, "missing_preview": [MARKER], "violations": [MARKER]})
    with engine.begin() as conn:
        from app.clhear.platform.events import emit
        emit(conn, layer="l1", kind="IngestFidelityFailed", subject_ref=KEY, producer="l1.pipeline",
             payload={"source": KEY, "source_version_id": version_id, "coverage": 0.5,
                      "error": str(error), "violations": [MARKER], "missing_preview": [MARKER]})
        conn.execute(eval_runs.insert().values(suite="e1_fidelity", source_key=KEY, passed=False,
                     scores={"source_version_id": version_id, "content_hash": version["content_hash"],
                             "score": 0.5, "error": str(error), "violations": [MARKER]}))
    return job, task, run.run_id


@pytest.mark.parametrize("revoked", [False, True])
def test_direct_db_evidence_never_reveals_raw_failure_text_and_keeps_database_evidence(engine, restricted_client, monkeypatch, revoked):
    job, task_id, run_id = recorded_failure(engine)
    if revoked:
        activate(engine, action="revoke", command_id="owner-revoked-evidence-test")
    _session(restricted_client)
    monkeypatch.setattr("app.clhear.l1.operator_access.verify_current_access",
                        lambda *a, **kw: pytest.fail("Metadata evidence must not request text authorization"))
    paths = [f"/api/clhear/runs/{run_id}", f"/api/clhear/l1/workflow?job_id={job}",
             f"/api/clhear/sources/{KEY}/evals", "/api/clhear/fleet", "/api/clhear/activity",
             f"/api/clhear/jobs/{job}", "/api/clhear/jobs/latest"]
    payloads = {}
    for path in paths:
        response = restricted_client.get(path)
        assert response.status_code == 200, (path, response.text)
        assert MARKER not in response.text, path
        payloads[path] = response.json()
    run = payloads[paths[0]]
    assert run["status"] == "failed" and run["outputs"]["coverage"] == 0.5
    assert run["inputs"] == {"source": KEY, "job_id": job}
    assert run["outputs"]["error_type"] == "RuntimeError"
    assert run["stages"][0]["duration_ms"] == 123
    assert run["stages"][0]["measurement"] == "interval_between_reports" and run["stages"][0]["ts"]
    report = payloads[paths[1]]
    assert report["workflow_stages"] == workflow.WORKFLOW_STAGES
    assert report["tasks"][0]["attempt"] == 1 and report["tasks"][0]["task_id"] == task_id
    assert report["steps"][0]["duration_ms"] >= 0 and report["steps"][0]["started_at"]
    assert report["task_total"] == 1 and report["pagination"]["tasks"]["has_more"] is False
    assert report["steps"][0]["details"]["error_type"] == "RuntimeError"
    scorecard = payloads[paths[2]]["scorecard"]
    assert scorecard["suites"]["e1_fidelity"]["scores"]["score"] == 0.5
    # Redaction is a read projection, never a rewrite of worker history.
    with engine.connect() as conn:
        assert MARKER in conn.execute(sa.select(workflow.tasks.c.error).where(workflow.tasks.c.task_id == task_id)).scalar_one()
        assert MARKER in json.dumps(conn.execute(sa.select(workflow.steps.c.details).where(workflow.steps.c.job_id == job)).scalar_one())
        assert MARKER in json.dumps(conn.execute(sa.select(runs.c.outputs).where(runs.c.id == run_id)).scalar_one())
        assert MARKER in json.dumps(conn.execute(sa.select(eval_runs.c.scores)).scalar_one())
        assert MARKER in json.dumps(conn.execute(sa.select(events.c.payload).where(events.c.kind == "IngestFidelityFailed")).scalar_one())

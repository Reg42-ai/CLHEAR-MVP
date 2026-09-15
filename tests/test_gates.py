"""Evals as gates (HLD v2 §3 "Evals", I10; CLHEAR-0.27): Langfuse self-hosted for
maintainers, per-layer golden sets, and a *published summary table* that is the
only thing the public dashboard reads."""
from __future__ import annotations

import json
from pathlib import Path

import sqlalchemy as sa

from app.clhear.models import eval_runs
from app.clhear.platform import evals, gates, langfuse

ROOT = Path(__file__).resolve().parents[1]


def test_summary_table_published(engine, client, monkeypatch):
    # An eval run lands in eval_runs; the summary table is derived from it per layer.
    evals.run_suite(engine, "l0_smoke", release="2026.09.13")
    summary = gates.publish_summary(engine, "2026.09.13")
    assert summary["release"] == "2026.09.13" and set(summary["layers"]) == set(gates.LAYER_GATES)
    for layer, st in summary["layers"].items():
        assert set(st) == {"passed", "thresholds", "suites", "missing"}
        for s, r in st["suites"].items():
            assert set(r) == {"passed", "scores", "ran_at"} and s in gates.LAYER_GATES[layer]
    l0 = summary["layers"]["L0"]
    assert "l0_smoke" in l0["suites"] and l0["suites"]["l0_smoke"]["passed"] is True

    # The public dashboard reads the summary table — the same function, nothing else.
    body = client.get("/evals/summary", params={"release": "2026.09.13"}).json()
    assert body["layers"] == json.loads(json.dumps(summary["layers"], default=str))
    page = client.get("/evals").text
    assert "/evals/summary" in page and "langfuse" not in page.lower()
    src = (ROOT / "app/clhear/api_keys.py").read_text()
    assert "langfuse" not in src.lower()  # the public route never touches the eval store or Langfuse

    # The release job publishes the same table as an artifact for the public repo.
    rel = (ROOT / ".github/workflows/release.yml").read_text()
    assert "app.clhear.workers --once --envelope-file" in rel
    assert "app.clhear.fleets nightly" not in rel


def test_eval_runs_mirror_to_langfuse_when_configured_and_never_carry_text(engine, monkeypatch):
    sent: list[dict] = []

    class Client:
        def post(self, url, json, auth):
            sent.append({"url": url, "batch": json["batch"], "auth": auth})

            class R:
                status_code = 207
                text = ""

            return R()

    monkeypatch.delenv("LANGFUSE_HOST", raising=False)
    assert langfuse.configured() is False and langfuse.export_run({"suite": "l0_smoke", "scores": {}}) is False

    monkeypatch.setenv("LANGFUSE_HOST", "https://evals.clhear.reg42.ai/")
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "pk-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "CHANGEME")
    assert langfuse.configured() is False  # SSM placeholder = off

    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "sk-test")
    record = {"suite": "l2_precision", "release": "2026.09.13", "source_key": "uksi/2017/692",
              "scores": {"precision": 0.97, "n": 40, "ok": True, "note": "text that must not travel"}, "passed": True,
              "ran_at": "2026-09-13T00:00:00+00:00"}
    assert langfuse.export_run(record, client=Client()) is True
    assert sent[0]["url"] == "https://evals.clhear.reg42.ai/api/public/ingestion" and sent[0]["auth"] == ("pk-test", "sk-test")
    batch = sent[0]["batch"]
    trace = [e for e in batch if e["type"] == "trace-create"]
    scores = {e["body"]["name"]: e["body"]["value"] for e in batch if e["type"] == "score-create"}
    assert len(trace) == 1 and "L2" in trace[0]["body"]["tags"] and trace[0]["body"]["metadata"]["suite"] == "l2_precision"
    assert scores == {"precision": 0.97, "n": 40.0, "ok": 1.0, "passed": 1.0}  # numeric only; strings never leave
    assert "text that must not travel" not in json.dumps(batch)
    assert all(e["body"]["traceId"] == trace[0]["body"]["id"] for e in batch if e["type"] == "score-create")


def test_golden_sets_exist_per_layer_and_infra_hosts_langfuse():
    layers = {p.name for p in (ROOT / "clhear-evals").iterdir() if p.is_dir()}
    assert {"l1", "l2", "l4", "l5", "l6", "l7"} <= layers
    tf = (ROOT / "infra/langfuse.tf").read_text()
    assert 'resource "aws_ecs_service" "langfuse"' in tf and "AUTH_DISABLE_SIGNUP" in tf and "langfuse_url" in tf
    ecs = (ROOT / "infra/ecs.tf").read_text()
    assert "LANGFUSE_HOST" in ecs and "LANGFUSE_SECRET_KEY" in ecs
    # the web tier (public dashboard) is deliberately NOT given Langfuse credentials
    assert "LANGFUSE" not in (ROOT / "infra/webui.tf").read_text()


def test_summary_reflects_a_failed_suite_as_a_closed_gate(engine):
    with engine.begin() as conn:
        conn.execute(eval_runs.insert().values(suite="l0_smoke", source_key=None, release="r-fail", scores={"tables": 0}, passed=False))
    summary = gates.publish_summary(engine, "r-fail")
    assert summary["layers"]["L0"]["passed"] is False and summary["layers"]["L0"]["suites"]["l0_smoke"]["passed"] is False
    with engine.connect() as conn:
        n = conn.execute(sa.select(sa.func.count()).select_from(eval_runs).where(eval_runs.c.release == "r-fail")).scalar()
    assert n == 1

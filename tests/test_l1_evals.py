"""L1 publication gates (HLD v2 §4.1): currency, boundary F1, family completeness."""
import json
from datetime import datetime, timedelta, timezone

import sqlalchemy as sa

from app.clhear.l1 import pipeline
from app.clhear.l1.models import source_versions, sources
from app.clhear.platform import evals
from app.clhear.platform.gates import LAYER_GATES
from tests.test_l1_synthetic_amendment import V1, V2, SyntheticAdapter


def test_l1_gate_lists_the_hld_suites():
    assert {"e1_fidelity", "e7_closure", "l1_family_completeness", "l1_currency", "l1_boundary_f1"} <= set(LAYER_GATES["L1"])
    assert set(LAYER_GATES["L1"]) <= set(evals.SUITES)


def test_boundary_f1_golden_set_passes():
    scores, passed = evals.l1_boundary_f1(None, None)
    assert passed, scores
    assert scores["cases"] >= 12
    assert scores["mean_f1"] >= evals.BOUNDARY_F1_THRESHOLD
    adapters = {r["adapter"] for r in scores["results"]}
    assert {"fca_handbook", "finra", "sec_edgar", "esma", "fatf", "bis_basel", "iosco", "mas", "asic", "isa", "irs_gov"} <= adapters
    assert all(r["f1"] == 1.0 for r in scores["results"]), [r for r in scores["results"] if r["f1"] < 1.0]


def test_boundary_f1_missing_golden_set_fails_honestly(monkeypatch, tmp_path):
    monkeypatch.setenv("CLHEAR_EVALS_DIR", str(tmp_path / "nowhere"))
    scores, passed = evals.l1_boundary_f1(None, None)
    assert passed is False and scores["cases"] == 0


def test_boundary_f1_math():
    perfect = evals.boundary_f1({"a", "b"}, {"a", "b"})
    assert perfect["f1"] == 1.0
    half = evals.boundary_f1({"a", "b"}, {"a", "c"})
    assert 0 < half["f1"] < 1.0
    assert evals.boundary_f1(set(), set())["f1"] in (0.0, 1.0)


def test_currency_gate(engine, tmp_path):
    # Empty corpus: honest fail (no tier-A version at all).
    scores, passed = evals.l1_currency(engine, None)
    assert passed is False and "error" in scores

    store = pipeline.LocalStore(tmp_path / "lake")
    # Tier-A adapter key so the suite counts it.
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01", adapter="uk_legislation", source_key="synthetic/tier-a"), store)
    scores, passed = evals.l1_currency(engine, None)
    assert passed is True
    assert scores["tier_a_sources"] == 1 and scores["median_lag_hours"] == 0.0  # first ingest = backfill

    # Second version picked up 3 days after our previous probe: late.
    pipeline.ingest(engine, SyntheticAdapter(V2, "2026-06-01", adapter="uk_legislation", source_key="synthetic/tier-a"), store)
    with engine.begin() as conn:
        src_id = conn.execute(sa.select(sources.c.id).where(sources.c.key == "synthetic/tier-a")).scalar_one()
        ids = conn.execute(
            sa.select(source_versions.c.id).where(source_versions.c.source_id == src_id).order_by(source_versions.c.id)
        ).scalars().all()
        base = datetime(2026, 6, 1, tzinfo=timezone.utc)
        conn.execute(source_versions.update().where(source_versions.c.id == ids[0]).values(retrieved_at=base, as_of_date=base.date()))
        conn.execute(
            source_versions.update().where(source_versions.c.id == ids[1])
            .values(retrieved_at=base + timedelta(days=3), as_of_date=base.date())
        )
    scores, passed = evals.l1_currency(engine, None)
    assert passed is False
    assert scores["max_lag_hours"] >= 72.0

    # Publisher back-dates a consolidation months earlier than our last probe:
    # we cannot have seen it before the probe, so lag counts from the probe.
    with engine.begin() as conn:
        conn.execute(
            source_versions.update().where(source_versions.c.id == ids[1])
            .values(retrieved_at=base + timedelta(hours=6), as_of_date=(base - timedelta(days=90)).date())
        )
    scores, passed = evals.l1_currency(engine, None)
    assert passed is True and scores["max_lag_hours"] <= 6.0


def test_family_completeness_gate(engine, tmp_path):
    scores, passed = evals.l1_family_completeness(engine, None)
    assert passed is False and scores["families"] == 0

    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, SyntheticAdapter(V1, "2026-01-01"), store)
    scores, passed = evals.l1_family_completeness(engine, None)
    assert passed is True, scores
    assert scores["families"] == 1 and scores["failing"] == []

    # A clause citing an instrument nobody has registered opens a discovery
    # candidate: completeness drops below 99 % and the gate fails honestly.
    cited = dict(V2)
    cited["5"] = "Firms must also comply with Regulation (EU) 2016/679 and the Financial Services Act 2012."
    pipeline.ingest(engine, SyntheticAdapter(cited, "2026-06-01"), store)
    scores, passed = evals.l1_family_completeness(engine, None)
    assert passed is False, scores
    assert scores["failing"] == ["synthetic-family"]
    worst = scores["worst"]
    assert worst["open_candidates"] >= 1


def test_run_all_records_every_gate_suite(engine):
    records = evals.run_all(engine, release="gate-test")
    suites = {r["suite"] for r in records}
    assert set(evals.GLOBAL_SUITES) <= suites
    assert {"l1_family_completeness", "l1_currency", "l1_boundary_f1"} <= suites
    by = {r["suite"]: r for r in records}
    assert by["l1_boundary_f1"]["passed"] is True
    assert by["l1_currency"]["passed"] is False
    assert json.dumps(by["l1_boundary_f1"]["scores"])  # serialisable artefact

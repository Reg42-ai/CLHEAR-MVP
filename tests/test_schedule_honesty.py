"""Phase A0: promised schedules must be kept or loudly failed."""
import sqlalchemy as sa

from app.clhear.l1.registry_etoro import S, seed
from app.clhear.models import runs
from app.clhear.platform.evals import run_suite


def test_schedule_kept_fails_when_nothing_attempted(engine):
    record = run_suite(engine, "l1_schedule_kept")
    assert record["passed"] is False
    assert record["scores"]["missed_count"] > 0
    assert record["scores"]["attempted_24h"] == 0


def test_schedule_kept_passes_when_every_source_attempted(engine):
    with engine.begin() as conn:
        for entry in S:
            conn.execute(
                runs.insert().values(
                    fleet=f"l1.{entry['adapter']}",
                    trigger="schedule",
                    inputs={"source": entry["key"]},
                    outputs={"status": "unchanged"},
                )
            )
    record = run_suite(engine, "l1_schedule_kept")
    assert record["passed"] is True, record["scores"]["missed"][:5]
    assert record["scores"]["missed_count"] == 0


def test_fleet_board_reports_schedule_missed(client, engine):
    seed(engine)
    board = client.get("/api/clhear/fleet").json()
    scheduled_no_run = [b for b in board if b["library_status"] == "schedule-missed"]
    assert scheduled_no_run, "sources with a promised schedule and no run must be schedule-missed"
    sample = scheduled_no_run[0]
    assert sample["last_attempted"] is None
    assert sample["attempted_24h"] is False
    assert sample["next_run_utc"] is not None


def test_fleet_board_ingested_source_shows_last_attempted(client, engine):
    seed(engine)
    with engine.begin() as conn:
        key = S[0]["key"]
        conn.execute(
            runs.insert().values(
                fleet=f"l1.{S[0]['adapter']}",
                trigger="schedule",
                inputs={"source": key},
                outputs={"status": "unchanged"},
            )
        )
    board = client.get("/api/clhear/fleet").json()
    row = next(b for b in board if b["source_key"] == S[0]["key"])
    assert row["last_attempted"] is not None
    assert row["attempted_24h"] is True
    assert row["library_status"] != "schedule-missed"

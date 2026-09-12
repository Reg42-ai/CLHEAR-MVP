"""Live-inference wiring: SSM hydrate, rehearsal skip."""
from datetime import datetime, timezone

import sqlalchemy as sa

from app.clhear.fleets import FLEET_RUN, _already_ran_today
from app.clhear.models import runs
from app.clhear.secrets import hydrate_ssm_env


def test_hydrate_ssm_fills_changeme_and_skips_fake():
    env = {"INFER_TOKEN": "CHANGEME", "CLHEAR_LLM_PROVIDER": ""}
    filled = hydrate_ssm_env(environ=env, getter=lambda name: "infer-test" if "INFER" in name else "")
    assert env["INFER_TOKEN"] == "infer-test"
    assert filled["INFER_TOKEN"] == "/clhear/INFER_TOKEN"

    fake_env = {"INFER_TOKEN": "CHANGEME", "CLHEAR_LLM_PROVIDER": "fake"}
    assert hydrate_ssm_env(environ=fake_env, getter=lambda _n: "infer-test") == {}
    assert fake_env["INFER_TOKEN"] == "CHANGEME"


def test_rehearsal_does_not_count_as_already_ran(engine):
    now = datetime.now(timezone.utc)
    with engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                fleet=FLEET_RUN, trigger="rehearsal", inputs={"trigger": "rehearsal"},
                outputs={}, created_at=now,
            )
        )
    assert _already_ran_today(engine) is False
    with engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                fleet=FLEET_RUN, trigger="schedule", inputs={}, outputs={}, created_at=now,
            )
        )
    assert _already_ran_today(engine) is True

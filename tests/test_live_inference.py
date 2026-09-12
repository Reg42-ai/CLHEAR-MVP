"""Live-inference wiring: SSM hydrate, rehearsal skip, GPU router bind."""
from datetime import datetime, timezone

import sqlalchemy as sa

from app.clhear.fleets import FLEET_RUN, _already_ran_today
from app.clhear.models import runs
from app.clhear.platform import gpu as gpu_mod
from app.clhear.platform.gateway import FakeProvider
from app.clhear.platform.router import Router
from app.clhear.secrets import hydrate_ssm_env


def test_hydrate_ssm_fills_changeme_and_skips_fake():
    env = {"OLLAMA_API_KEY": "CHANGEME", "CLHEAR_LLM_PROVIDER": ""}
    filled = hydrate_ssm_env(environ=env, getter=lambda name: "ollama-test" if "OLLAMA" in name else "")
    assert env["OLLAMA_API_KEY"] == "ollama-test"
    assert filled["OLLAMA_API_KEY"] == "/clhear/OLLAMA_API_KEY"

    fake_env = {"OLLAMA_API_KEY": "CHANGEME", "CLHEAR_LLM_PROVIDER": "fake"}
    assert hydrate_ssm_env(environ=fake_env, getter=lambda _n: "ollama-test") == {}
    assert fake_env["OLLAMA_API_KEY"] == "CHANGEME"


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


def test_attach_router_points_at_gpu_url(engine):
    fake = FakeProvider()
    llm = Router(engine, providers={"ollama": fake}, gpu_open=False)
    previous = gpu_mod.attach_router(llm, "http://10.0.1.20:11434")
    assert llm._gpu_open is True
    assert llm.providers["ollama"].name == "ollama"
    assert llm.providers["ollama"]._base_url == "http://10.0.1.20:11434"
    gpu_mod.detach_router(llm, previous)
    assert llm.providers["ollama"] is fake
    assert llm._gpu_open is False

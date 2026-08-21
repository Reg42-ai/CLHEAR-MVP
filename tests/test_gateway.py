import pytest
import sqlalchemy as sa

from app.clhear.models import llm_calls
from app.clhear.platform.gateway import FakeProvider, Gateway, SpendCapExceeded, StructuredOutputError


def test_every_call_logged_with_hash_and_cost(engine):
    gateway = Gateway(engine, FakeProvider())
    gateway.call(fleet="dummy", model="m", prompt="hello")
    with engine.connect() as conn:
        row = conn.execute(sa.select(llm_calls)).one()
    assert row.provider == "fake"
    assert len(row.prompt_hash) == 64
    assert float(row.cost_usd) > 0


def test_fleet_cap_is_a_hard_stop(engine):
    provider = FakeProvider()
    gateway = Gateway(engine, provider, fleet_daily_cap_usd=0.0)
    with pytest.raises(SpendCapExceeded):
        gateway.call(fleet="dummy", model="m", prompt="hello")
    assert provider.calls == 0  # blocked before reaching the provider


def test_global_cap_spans_fleets(engine):
    gateway = Gateway(engine, FakeProvider(), fleet_daily_cap_usd=1000, global_daily_cap_usd=0.00005)
    gateway.call(fleet="a", model="m", prompt="hello")
    with pytest.raises(SpendCapExceeded):
        gateway.call(fleet="b", model="m", prompt="hello")


def test_structured_output_validation(engine):
    gateway = Gateway(engine, FakeProvider(canned_text="not json"))
    with pytest.raises(StructuredOutputError):
        gateway.call(fleet="dummy", model="m", prompt="p", required_keys=["classification"], max_retries=1)
    ok = Gateway(engine, FakeProvider()).call(
        fleet="dummy", model="m", prompt="p", required_keys=["classification", "confidence"]
    )
    assert ok.text

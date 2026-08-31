import json

import pytest
import sqlalchemy as sa

from app.clhear.models import llm_calls
from app.clhear.platform.gateway import (
    FakeProvider,
    Gateway,
    SpendCapExceeded,
    StructuredOutputError,
    parse_json_object,
)


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
    assert json.loads(ok.text) == {"classification": "relevant", "confidence": 0.9}

    messy = Gateway(
        engine,
        FakeProvider(canned_text='<think>hmm</think>\n```json\n{"classification": "x", "confidence": 0.5}\n```'),
    ).call(
        fleet="dummy", model="m", prompt="p", required_keys=["classification", "confidence"]
    )
    assert json.loads(messy.text) == {"classification": "x", "confidence": 0.5}


def test_ollama_omits_bare_json_format(monkeypatch):
    from app.clhear.platform.gateway import OllamaProvider

    seen = {}

    class _Resp:
        def raise_for_status(self):
            return None

        def json(self):
            return {"response": '{"ok": true}', "thinking": "", "prompt_eval_count": 1, "eval_count": 1}

    def fake_post(url, json=None, timeout=None):
        seen["body"] = json
        return _Resp()

    monkeypatch.setattr("httpx.post", fake_post)
    OllamaProvider("http://127.0.0.1:11434").complete(
        model="qwen3.5:4b", prompt="hi", system=None, max_tokens=16,
    )
    assert seen["body"]["think"] is False
    assert "format" not in seen["body"]
    OllamaProvider("http://127.0.0.1:11434").complete(
        model="qwen3.5:4b", prompt="hi", system=None, max_tokens=16,
        json_schema={"type": "object"},
    )
    assert seen["body"]["format"] == {"type": "object"}


def test_parse_json_object_strips_think_and_fences():
    body = {"is_duty": True, "evidence_span": "keep records"}
    wrapped = "<think>hmm</think>\n```json\n" + json.dumps(body) + "\n```"
    assert parse_json_object(wrapped) == body
    assert parse_json_object("prefix " + json.dumps(body) + " trailing") == body

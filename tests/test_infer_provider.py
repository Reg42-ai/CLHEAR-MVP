"""InferProvider: the only production provider (HLD v2 I6). Wire contract with
Reg42 Infer — headers, body, resolved model id, cost, route/explain."""
import json

import pytest

from app.clhear.platform import task_classes as tc
from app.clhear.platform.gateway import BEDROCK_PRICING, InferError, InferProvider, price_for


class _Resp:
    def __init__(self, status_code, payload):
        self.status_code = status_code
        self._payload = payload
        self.text = json.dumps(payload)

    def json(self):
        return self._payload


class _Client:
    def __init__(self, responses):
        self.responses = list(responses)
        self.calls = []

    def post(self, url, headers=None, json=None):
        self.calls.append({"method": "POST", "url": url, "headers": headers, "json": json})
        return self.responses.pop(0)

    def get(self, url, headers=None, params=None):
        self.calls.append({"method": "GET", "url": url, "headers": headers, "params": params})
        return self.responses.pop(0)


def _provider(responses, **kw):
    client = _Client(responses)
    p = InferProvider("https://infer.reg42.ai/v1/", "tok", employee_id="clhear-l2", data_class="public", client=client, **kw)
    return p, client


def test_complete_sends_task_headers_and_records_resolved_model():
    p, client = _provider([
        _Resp(200, {
            "model": tc.MISTRAL_LARGE_3,  # Infer fell through the ladder
            "choices": [{"message": {"content": '{"ok": true}'}}],
            "usage": {"prompt_tokens": 1000, "completion_tokens": 500},
        })
    ])
    res = p.complete(
        model=tc.GPT_OSS_120B, prompt="p", system="s", max_tokens=64, temperature=0.0,
        json_schema={"type": "object"}, task_class="l2_extract",
    )
    call = client.calls[0]
    assert call["url"] == "https://infer.reg42.ai/v1/chat/completions"
    assert call["headers"]["authorization"] == "Bearer tok"
    assert call["headers"]["X-Employee-Id"] == "clhear-l2"
    assert call["headers"]["X-Task-Class"] == "l2_extract"
    assert call["headers"]["X-Data-Class"] == "public"
    body = call["json"]
    assert body["model"] == tc.GPT_OSS_120B
    assert body["messages"][0] == {"role": "system", "content": "s"}
    assert body["response_format"]["type"] == "json_schema"
    assert body["metadata"]["task_class"] == "l2_extract"
    assert res.provider == "infer"
    assert res.model == tc.MISTRAL_LARGE_3  # what actually ran, for the ledger + manifest
    price_in, price_out = BEDROCK_PRICING[tc.MISTRAL_LARGE_3]
    assert res.cost_usd == pytest.approx((1000 * price_in + 500 * price_out) / 1_000_000)


def test_complete_prefers_infer_cost_when_reported():
    p, _ = _provider([
        _Resp(200, {"model": tc.NOVA_LITE, "choices": [{"message": {"content": "{}"}}],
                    "usage": {"prompt_tokens": 10, "completion_tokens": 10, "cost_usd": 0.0042}})
    ])
    res = p.complete(model=tc.NOVA_LITE, prompt="p", system=None, max_tokens=8)
    assert res.cost_usd == pytest.approx(0.0042)


def test_http_errors_raise_infer_error():
    p, _ = _provider([_Resp(429, {"error": "rate limited"})])
    with pytest.raises(InferError):
        p.complete(model=tc.NOVA_LITE, prompt="p", system=None, max_tokens=8)
    p, _ = _provider([_Resp(200, {"nope": True})])
    with pytest.raises(InferError):
        p.complete(model=tc.NOVA_LITE, prompt="p", system=None, max_tokens=8)


def test_route_explain_contract():
    p, client = _provider([
        _Resp(200, {"task_class": "l2_extract", "ladder": tc.default_ladder("l2_extract"), "selected": tc.GPT_OSS_120B})
    ])
    ex = p.route_explain("l2_extract")
    assert ex["selected"] == tc.GPT_OSS_120B
    call = client.calls[0]
    assert call["method"] == "GET" and call["url"].endswith("/route/explain")
    assert call["params"]["task_class"] == "l2_extract"
    assert call["headers"]["X-Task-Class"] == "l2_extract"


def test_requires_configuration(monkeypatch):
    from app.clhear.settings import get_settings

    monkeypatch.setenv("INFER_BASE_URL", "")
    monkeypatch.setenv("INFER_TOKEN", "")
    get_settings.cache_clear()
    with pytest.raises(RuntimeError):
        InferProvider()
    with pytest.raises(RuntimeError):
        InferProvider("https://infer.reg42.ai/v1", "CHANGEME")
    get_settings.cache_clear()


def test_pricing_covers_every_ladder_model():
    for name in tc.TASK_CLASSES:
        for model in tc.default_ladder(name):
            assert model in BEDROCK_PRICING, model
    assert price_for("unknown-model") == (3.00, 15.00)


def test_only_infer_and_fake_providers_exist():
    import inspect

    from app.clhear.platform import gateway

    providers = [
        n for n, obj in inspect.getmembers(gateway, inspect.isclass)
        if n.endswith("Provider") and obj.__module__ == gateway.__name__ and n != "Provider"
    ]
    assert sorted(providers) == ["FakeProvider", "InferProvider"]

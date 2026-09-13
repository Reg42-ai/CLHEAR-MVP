"""HLD v2 §6 — the change digest goes to the beehiiv newsletter; inert when unconfigured."""
from __future__ import annotations

from datetime import date

from app.clhear.platform import newsletter
from app.clhear.settings import get_settings


def _configure(monkeypatch, status="draft"):
    monkeypatch.setenv("CLHEAR_BEEHIIV_API_KEY", "bh-test-key")
    monkeypatch.setenv("CLHEAR_BEEHIIV_PUBLICATION_ID", "pub_123")
    monkeypatch.setenv("CLHEAR_BEEHIIV_POST_STATUS", status)
    get_settings.cache_clear()


def test_inert_when_unconfigured(engine, monkeypatch):
    monkeypatch.delenv("CLHEAR_BEEHIIV_API_KEY", raising=False)
    monkeypatch.delenv("CLHEAR_BEEHIIV_PUBLICATION_ID", raising=False)
    get_settings.cache_clear()
    assert newsletter.configured() is False
    out = newsletter.send_digest(engine)
    assert out["sent"] is False and "not configured" in out["reason"]
    assert newsletter.subscribe("ada@example.com")["subscribed"] is False


def test_compose_digest_is_an_honest_empty_feed(engine):
    d = newsletter.compose_digest(engine, date(2026, 1, 1), base_url="https://clhear.example")
    assert d["count"] == 0 and "no changes" in d["subject"].lower()
    assert "https://clhear.example/watch" in d["text"] and "/contribute" in d["text"]
    assert "<h1>" in d["html"] and "<script" not in d["html"]


def test_send_digest_posts_a_draft_and_skips_empty_windows(engine, monkeypatch):
    _configure(monkeypatch)
    fake = newsletter.FakeTransport()
    skipped = newsletter.send_digest(engine, transport=fake)
    assert skipped["sent"] is False and "no changes" in skipped["reason"] and fake.calls == []
    forced = newsletter.send_digest(engine, transport=fake, skip_empty=False)
    assert forced["sent"] is True and forced["status"] == "draft" and forced["post_id"] == "post_1"
    call = fake.calls[0]
    assert call["method"] == "POST" and call["url"] == "https://api.beehiiv.com/v2/publications/pub_123/posts"
    assert call["headers"]["Authorization"] == "Bearer bh-test-key"
    assert call["body"]["status"] == "draft" and call["body"]["title"] and "<h1>" in call["body"]["body_content"]
    get_settings.cache_clear()


def test_confirmed_status_and_subscribe(engine, monkeypatch):
    _configure(monkeypatch, status="confirmed")
    fake = newsletter.FakeTransport()
    out = newsletter.send_digest(engine, transport=fake, skip_empty=False)
    assert out["status"] == "confirmed" and fake.calls[0]["body"]["status"] == "confirmed"
    sub = newsletter.subscribe("Ada@Example.com", transport=fake)
    assert sub["subscribed"] is True and sub["email"] == "ada@example.com"
    assert fake.calls[1]["url"].endswith("/publications/pub_123/subscriptions")
    assert fake.calls[1]["body"]["email"] == "ada@example.com" and fake.calls[1]["body"]["reactivate_existing"] is True
    get_settings.cache_clear()


def test_transport_errors_never_raise(engine, monkeypatch):
    _configure(monkeypatch)

    def boom(*_a, **_k):
        raise RuntimeError("beehiiv 503")

    out = newsletter.send_digest(engine, transport=boom, skip_empty=False)
    assert out["sent"] is False and "503" in out["reason"]
    assert newsletter.subscribe("x@y.org", transport=boom)["subscribed"] is False
    get_settings.cache_clear()

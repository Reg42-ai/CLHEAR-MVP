"""The latency gate fails a route that is slow or answers 5xx, and prints no credential."""
from app.clhear import latency_probe


def test_budgets_decide_the_gate(monkeypatch, capsys):
    monkeypatch.setattr(latency_probe, "_headers", lambda kind: {"Cookie": "clhear_session=secret-cookie"})
    timings = {"/api/clhear/layers": (200, 1500.0), "/l2": (502, 20.0)}
    monkeypatch.setattr(latency_probe, "_get", lambda url, headers: timings.get(url.split("testserver")[1], (200, 10.0)))
    report = latency_probe.probe("https://testserver", rounds=3)
    by_path = {r["path"]: r for r in report["routes"]}
    assert report["passed"] is False
    assert by_path["/api/clhear/layers"]["passed"] is False and by_path["/api/clhear/layers"]["p95_ms"] == 1500
    assert by_path["/l2"]["passed"] is False and by_path["/l2"]["statuses"] == ["502"]
    assert by_path["/signin"]["passed"] is True
    assert "secret-cookie" not in str(report)

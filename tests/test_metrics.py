"""Observability and security surface (HLD v2 §7.1; items 10 and 17): status.json with
SLOs measured from the data, Prometheus exposition, the public status and security
pages, RFC 9116 security.txt, and enterprise SSO routing by email domain."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

from app.clhear import curated
from app.clhear.accounts import saml_provider_for
from app.clhear.platform import metrics
from app.clhear.settings import get_settings
from app.clhear.v1 import security


def test_status_measures_freshness_and_gates_from_the_data(engine):
    curated.seed(engine)
    now = datetime.now(timezone.utc)
    st = metrics.status(engine, now=now)
    assert st["status"] in {"operational", "degraded", "outage"} and st["api"]["up"] is True
    fresh = st["freshness"]
    assert set(fresh) == set(metrics.LAYERS)
    assert fresh["L1"]["empty"] and fresh["L1"]["age_seconds"] is None and not fresh["L1"]["ok"]  # nothing ingested → not fresh, honestly
    assert fresh["L4"]["empty"] is False and fresh["L4"]["ok"] and fresh["L4"]["slo_seconds"] == metrics.DERIVED_FRESHNESS_SLO_S
    # time passing makes a layer stale without anyone touching the data
    later = metrics.status(engine, now=now + timedelta(days=3))
    assert later["freshness"]["L4"]["ok"] is False
    slos = {s["name"]: s for s in st["slos"]}
    assert set(slos) == {s["name"] for s in metrics.SLOS}
    assert slos["freshness_tier_a"]["met"] is False and slos["api_availability"]["met"] is True
    assert set(st["gates"]) and all({"passed", "missing", "suites"} <= set(g) for g in st["gates"].values())
    assert "audit" in st["last_24h"] and "fleet_runs" in st["last_24h"]
    assert st["status"] != "operational"  # an empty L1 can never be operational


def test_status_reports_the_release_the_latest_pointer_names(engine, tmp_path, monkeypatch):
    """The status page names the release ``latest.json`` points at (what the API
    serves), not merely the newest folder; a store that cannot be read is an honest
    null rather than an exception."""
    from app.clhear import releases

    monkeypatch.setenv("CLHEAR_ARTIFACTS_DIR", str(tmp_path))
    monkeypatch.setenv("CLHEAR_RELEASES_S3_PREFIX", "")
    get_settings.cache_clear()
    try:
        curated.seed(engine)
        for rid, when in (("2026.01.01", "2026-01-01T00:00:00+00:00"), ("2026.02.02", "2026-02-02T00:00:00+00:00")):
            releases._put_json_local(releases._local_root() / rid / releases.MANIFEST_NAME, {"id": rid, "generated_at": when})
        releases._put_json_local(releases._local_root() / releases.LATEST_NAME, {"id": "2026.01.01"})
        rel = metrics.status(engine)["release"]
        assert rel == {"id": "2026.01.01", "generated_at": "2026-01-01T00:00:00+00:00"}

        monkeypatch.setattr(releases, "get_latest", lambda engine=None: (_ for _ in ()).throw(PermissionError("denied")))
        assert metrics.status(engine)["release"] == {"id": None, "generated_at": None}
    finally:
        get_settings.cache_clear()


def test_prometheus_exposition_is_well_formed(engine):
    curated.seed(engine)
    text = metrics.prometheus(engine)
    assert text.startswith("# HELP clhear_up") and "\nclhear_up 1\n" in text
    assert 'clhear_layer_freshness_seconds{layer="L1"} -1' in text
    assert 'clhear_layer_fresh{layer="L4"} 1' in text
    assert 'clhear_gate_passed{layer="L1"} 0' in text
    assert 'clhear_slo_met{slo="freshness_tier_a"} 0' in text
    for line in text.splitlines():
        assert line.startswith("#") or " " in line, line
    types = [l.split()[3] for l in text.splitlines() if l.startswith("# TYPE")]
    assert types and set(types) == {"gauge"}


def test_status_and_security_endpoints(client, engine):
    curated.seed(engine)
    st = client.get("/status.json")
    assert st.status_code in (200, 503) and st.headers["cache-control"] == "no-store" and st.json()["slos"]
    assert st.status_code == (200 if st.json()["status"] != "outage" else 503)
    page = client.get("/status")
    assert page.status_code == 200 and "/status.json" in page.text and 'lang="en"' in page.text
    m = client.get("/metrics")
    assert m.status_code == 200 and m.headers["content-type"].startswith("text/plain") and "clhear_up 1" in m.text

    txt = client.get("/.well-known/security.txt")
    assert txt.status_code == 200 and txt.headers["content-type"].startswith("text/plain")
    lines = txt.text.splitlines()
    assert lines[0] == f"Contact: mailto:{security.SECURITY_CONTACT}"
    fields = {l.split(":", 1)[0] for l in lines if l}
    assert {"Contact", "Expires", "Policy", "Canonical", "Acknowledgments", "Preferred-Languages"} <= fields
    expires = next(l for l in lines if l.startswith("Expires:")).split(" ", 1)[1]
    assert datetime.fromisoformat(expires.replace("Z", "+00:00")) > datetime.now(timezone.utc) + timedelta(days=300)
    base = get_settings().clhear_public_base_url.rstrip("/")
    assert f"Canonical: {base}/.well-known/security.txt" in lines

    posture = client.get("/security.json").json()
    assert posture["vdp"]["safe_harbour"] is True and posture["vdp"]["fix_sla_days"]["critical"] <= 7
    assert {"SOC2_TYPE_I_READINESS.md", "PENTEST_SCOPE.md", "VDP.md"} <= set(posture["documents"])
    assert "every read of licensed clause text" in posture["audit"]["what"]
    sec = client.get("/security")
    assert sec.status_code == 200 and "/security.json" in sec.text and "security.txt" in sec.text


def test_enterprise_sso_routes_by_email_domain(client, monkeypatch):
    monkeypatch.setenv("CLHEAR_SAML_DOMAINS", '{"AcmeBank": ["acme.example", "acme-group.example"], "Globex": "globex.example"}')
    get_settings.cache_clear()
    try:
        assert saml_provider_for("ada@acme.example") == "AcmeBank"
        assert saml_provider_for("ada@treasury.acme.example") == "AcmeBank"  # sub-domains inherit the enterprise IdP
        assert saml_provider_for("bob@globex.example") == "Globex"
        assert saml_provider_for("carol@gmail.example") is None and saml_provider_for("") is None
        doms = client.get("/auth/sso/domains").json()
        assert doms["providers"] == ["AcmeBank", "Globex"] and "acme-group.example" in doms["domains"]
        assert client.get("/auth/sso", params={"email": "carol@gmail.example"}).status_code == 404
        assert client.get("/auth/sso", params={"email": "nope"}).status_code == 422
        # a routed domain hands off to the pool's hosted UI; without a pool configured that is an honest 503
        assert client.get("/auth/sso", params={"email": "ada@acme.example"}, follow_redirects=False).status_code == 503
        monkeypatch.setenv("CLHEAR_COGNITO_REGION", "eu-west-1")
        monkeypatch.setenv("CLHEAR_COGNITO_USER_POOL_ID", "eu-west-1_TEST")
        monkeypatch.setenv("CLHEAR_COGNITO_CLIENT_ID", "client123")
        monkeypatch.setenv("CLHEAR_COGNITO_DOMAIN", "https://clhear-auth.auth.eu-west-1.amazoncognito.com")
        get_settings.cache_clear()
        r = client.get("/auth/sso", params={"email": "ada@acme.example"}, follow_redirects=False)
        assert r.status_code in (302, 307) and "identity_provider=AcmeBank" in r.headers["location"] and "/oauth2/authorize" in r.headers["location"]
    finally:
        get_settings.cache_clear()


def test_malformed_saml_map_is_ignored(monkeypatch):
    monkeypatch.setenv("CLHEAR_SAML_DOMAINS", "{not json")
    get_settings.cache_clear()
    try:
        assert get_settings().saml_domain_map == {} and saml_provider_for("x@y.example") is None
    finally:
        get_settings.cache_clear()

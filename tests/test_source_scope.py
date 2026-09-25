"""A named source scope narrows the registry and the fleet plan to exactly its documents."""
import pytest

from app.clhear.l1 import fleet, scopes, source_registry

SCOPE = "compliance-program-demo"


@pytest.fixture()
def scoped(monkeypatch):
    saved_s, saved_f = list(source_registry.S), list(source_registry.FAMILIES)
    monkeypatch.setenv(scopes.SCOPE_ENV, SCOPE)
    source_registry.apply_scope()
    yield
    source_registry.S[:] = saved_s
    source_registry.FAMILIES[:] = saved_f


def test_every_scoped_source_is_declared_or_a_starter():
    keys = set(scopes.source_keys(SCOPE))
    declared = {s["key"] for s in source_registry.S}
    assert keys - declared == {"nist/csf-2.0"}


def test_scope_components_and_roles_name_only_scoped_sources():
    scope = scopes.get(SCOPE)
    keys = set(scope["sources"])
    assert all(set(v) <= keys for v in scope["components"].values())
    assert all(set(v) <= keys for v in scope["roles"].values())
    assert set().union(*scope["imports"].values()) == keys


def test_registry_and_fleet_plan_hold_only_the_scope(scoped):
    keys = set(scopes.source_keys(SCOPE))
    assert {s["key"] for s in source_registry.S} == keys - {"nist/csf-2.0"}
    planned = {adapter.meta().source_key for _, adapter in fleet.fleet_plan()}
    assert planned == keys
    govinfo = {adapter.meta().source_key for _, adapter in fleet.fleet_plan("govinfo_us")}
    assert govinfo == set(scopes.get(SCOPE)["imports"]["govinfo_us"])
    assert {f[0] for f in source_registry.FAMILIES} == {s["family"] for s in source_registry.S}


def test_unknown_scope_fails_loudly(monkeypatch):
    monkeypatch.setenv(scopes.SCOPE_ENV, "no-such-scope")
    with pytest.raises(KeyError):
        scopes.active()


@pytest.mark.parametrize("key,adapter,pinned", [
    ("usc/15/80b-3", "GovInfoUscAdapter", "USCODE-2023-title15-chap2D-subchapII-sec80b-3.htm"),
    ("cfr/17/ia-compliance", "GovInfoEcfrAdapter", "section=275.206%284%29-7"),
    ("cfr/17/reg-sp-safeguards", "GovInfoEcfrAdapter", "part=248&section=248.30"),
    ("sec/staff/marketing-faq", "SecPageAdapter", None),
    ("sec/releases/ia-2204", "SecPageAdapter", None),
])
def test_new_sources_use_their_pinned_readers(key, adapter, pinned):
    entry = next(s for s in source_registry.S if s["key"] == key)
    built = fleet.adapter_for(entry)
    assert type(built).__name__ == adapter
    if adapter == "GovInfoUscAdapter":
        assert pinned in built._url_template.format(ed=built.edition, title=built.title, sec="80b-3")
    elif adapter == "GovInfoEcfrAdapter":
        url = built.section_url(entry["fetch"]["ecfr_sections"][0])
        assert pinned in url and "subchapter" not in url

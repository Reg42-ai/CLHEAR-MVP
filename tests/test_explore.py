"""HLD v2 §5 — layer browsers, why / history / who-else / request-a-change, compare, constellation.

Every public node is reachable from /explore without an account (I9); every
node page carries its why-trail (I3) and its bi-temporal history (I2).
"""
from __future__ import annotations

from urllib.parse import quote

from tests.test_l6_blueprints import BROKER, _corpus


def _blueprint(client, engine, tmp_path) -> dict:
    _corpus(engine, tmp_path)
    out = client.post("/solon/build", json={"attributes": BROKER, "name": "explore test"}).json()
    assert out["ok"], out
    return out


def test_explore_page_and_layer_counts(engine, client):
    page = client.get("/explore")
    assert page.status_code == 200 and 'id="main"' in page.text and "theme.css" in page.text
    layers = client.get("/explore/layers").json()["layers"]
    assert list(layers) == ["L1", "L2", "L3", "L4", "L5", "L6"]
    for layer, row in layers.items():
        assert row["count"] >= 0 and row["href"].startswith("/") and row["api"].startswith("/")
    assert layers["L4"]["licences"] >= 1  # the seeded register


def test_map_page_and_graph_canvas_are_served_and_linked(engine, client):
    """The Obsidian-style map (HLD v2 §5 constellation): its own page, the shared
    renderer module, and the explore node page drawing on the same canvas."""
    page = client.get("/map")
    assert page.status_code == 200 and 'id="main"' in page.text and "theme.css" in page.text
    assert "/static/graph-canvas.js" in page.text and "/graph/subgraph" in page.text
    for section in ("Filters", "Granularity", "Display", "Forces"):
        assert f"<summary>{section}</summary>" in page.text
    js = client.get("/static/graph-canvas.js")
    assert js.status_code == 200 and js.headers["content-type"].startswith("text/javascript")
    assert "export function createGraphCanvas" in js.text and "prefers-reduced-motion" in js.text
    for mod in ("d3-force@3.0.0", "d3-zoom@3.0.0", "d3-selection@3.0.0", "d3-drag@3.0.0"):
        assert f"https://esm.sh/{mod}" in js.text
    explore = client.get("/explore").text
    assert "/static/graph-canvas.js" in explore and '"/map#"' in explore and "<svg" not in explore
    css = client.get("/static/theme.css").text
    assert all(f"--l{i}:" in css for i in range(1, 9)) and ".gc-a11y" in css


def test_search_finds_nodes_across_layers(engine, client, tmp_path):
    _blueprint(client, engine, tmp_path)
    hits = client.get("/explore/search", params={"q": "client money"}).json()
    assert hits["count"] >= 1 and hits["q"] == "client money"
    kinds = {h["kind"] for h in hits["hits"]}
    assert kinds & {"obligation", "block", "activity", "licence"}
    for h in hits["hits"]:
        assert h["id"] and h["layer"] and h["href"].startswith("/")
    assert client.get("/explore/search", params={"q": ""}).status_code == 422
    assert client.get("/explore/search", params={"q": "zzz-nothing-matches"}).json()["count"] == 0


def test_node_page_has_why_history_who_else_and_a_change_request(engine, client, tmp_path):
    out = _blueprint(client, engine, tmp_path)
    ob = client.get("/explore/search", params={"q": "client money"}).json()["hits"]
    oid = next(h["id"] for h in ob if h["kind"] == "obligation")
    # derivation keys contain '#': the path segment must be percent-encoded (the SDKs and explore.html do this)
    node = client.get(f"/explore/node/{quote(oid, safe=':/')}").json()
    assert node["kind"] == "obligation" and node["layer"] == "L2" and node["node"]["id"] == oid
    assert node["why"], "I3: every derived node carries its why-trail"
    assert node["history"], "I2: history is never empty for a live node"
    assert "request_change" in node and node["request_change"]["method"] == "POST"
    assert isinstance(node["who_else"], (list, dict))
    # neighbours reach downward into L3 / L5 (I1) and upward into L1
    layers = {n["layer"] for n in node["neighbours"]}
    assert "L1" in layers and (layers & {"L3", "L5"})
    assert all(e["from"] and e["to"] and e["rel"] for e in node["edges"])
    # the blueprint's page is the evidence chain the front door links to
    bp = client.get(f"/explore/node/{out['blueprint_id']}").json()
    assert bp["kind"] == "blueprint" and bp["why"] and bp["history"]


def test_unknown_nodes_are_404(engine, client):
    assert client.get("/explore/node/OBL-does-not-exist").status_code == 404
    assert client.get("/explore/node/BLK-nope").status_code == 404
    assert client.get("/explore/graph", params={"focus": "BLU-nope"}).status_code == 404
    assert client.get("/explore/graph").status_code == 422


def test_constellation_graph_is_the_node_plus_one_hop(engine, client, tmp_path):
    out = _blueprint(client, engine, tmp_path)
    g = client.get("/explore/graph", params={"focus": out["blueprint_id"]}).json()
    assert g["focus"] == out["blueprint_id"] and g["kind"] == "blueprint"
    ids = {n["id"] for n in g["nodes"]}
    assert out["blueprint_id"] in ids
    for e in g["edges"]:
        assert e["from"] in ids and e["to"] in ids
    assert {n["layer"] for n in g["nodes"]} >= {"L6", "L4"}


def test_cross_jurisdiction_compare(engine, client, tmp_path):
    _blueprint(client, engine, tmp_path)
    cmp_ = client.get("/explore/compare", params={"jurisdictions": "uk,eu,us"})
    assert cmp_.status_code == 200, cmp_.text
    body = cmp_.json()
    assert body["jurisdictions"] == ["uk", "eu", "us"] and body["count"] == len(body["groups"])
    for g in body["groups"]:
        assert len(g["members"]) >= 2 and set(g["gaps"]) <= {"uk", "eu", "us"}
    assert client.get("/explore/compare", params={"jurisdictions": "uk"}).status_code == 422

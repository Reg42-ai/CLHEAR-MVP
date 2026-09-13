"""HLD v2 §5 — the Solon front door.

"Describe your organization" → at most three L4 questions → a validated
profile → a blueprint composed inside the 60 s budget with a progress
narrative per layer (not a spinner), the evidence chain one click away, and
no account, key or paywall anywhere on the path (I9).
"""
from __future__ import annotations

import time

from app.clhear import solon
from app.clhear.l4.validate import Ontology
from tests.test_l6_blueprints import BROKER, _corpus

GLOBAL_BROKER = ("We are a global retail equities broker holding client money, FCA authorised in the UK, "
                 "with EU and US licences serving retail clients online.")


def _walk(client, text: str) -> tuple[dict, int]:
    """Drive intake → answers the way the SDKs do (accept the suggested options)."""
    state = client.post("/solon/intake", json={"text": text}).json()
    asked: list[str] = []
    n = 0
    while not state["complete"]:
        n += 1
        suggested = state["suggested"] or [o["value"] for o in state["options"] if o.get("valid", True)][:1]
        value = suggested if state["type"] == "list" else suggested[0]
        r = client.post("/solon/answer", json={"attributes": state["attributes"], "attribute": state["attribute"],
                                                "value": value, "asked": asked, "merge": bool(state.get("merge"))})
        assert r.status_code == 200, r.text
        state = r.json()
        asked = state["asked"]
    return state, n


# --------------------------------------------------------------------------- reading


def test_intake_is_closed_world_and_never_assumes_a_licence(engine, client):
    body = client.post("/solon/intake", json={"text": GLOBAL_BROKER}).json()
    attrs = body["attributes"]
    assert attrs["jurisdictions"] == ["UK", "EU", "US"]
    assert set(attrs["products"]) == {"equities brokerage", "client money holding"}
    assert attrs["customer_base"] == ["retail"]
    # "holding client money" names the CASS permission; "broker" does NOT imply dealing / Part 4A
    assert attrs.get("authorisations") == ["Client money permission (CASS)"]
    # every fact carries the cue it was read from
    cues = {(e["attribute"], e["value"]): e["cue"] for e in body["evidence"]}
    assert cues[("products", "equities brokerage")] == "broker"
    assert cues[("jurisdictions", "UK")] == "uk"
    # the first question asks for the missing authorisations and offers those that permit the products
    assert body["complete"] is False and body["attribute"] == "authorisations"
    assert "Dealing in investments as agent" in body["suggested"]
    assert "SEC-registered broker-dealer (Exchange Act s.15(b))" in body["suggested"]
    assert body["questions_left"] == solon.MAX_QUESTIONS
    assert "never assumes a licence" in body["why"]


def test_unknown_text_asks_for_jurisdiction_first(engine, client):
    body = client.post("/solon/intake", json={"text": "We do things with money."}).json()
    assert body["attributes"] == {} and body["attribute"] == "jurisdictions" and body["complete"] is False
    assert {o["value"] for o in body["options"]} >= {"UK", "EU", "US"}


def test_answer_rejects_unresolvable_values_but_keeps_going(engine, client):
    state = client.post("/solon/intake", json={"text": "A UK broker."}).json()
    r = client.post("/solon/answer", json={"attributes": state["attributes"], "attribute": "authorisations",
                                            "value": ["Dealing in investments as agent", "Licence to print money"], "asked": []})
    body = r.json()
    assert body["rejected"] == ["Licence to print money"]
    assert body["attributes"]["authorisations"] == ["Dealing in investments as agent"]
    assert body["asked"] == ["authorisations"]
    r = client.post("/solon/answer", json={"attributes": {}, "attribute": "shoe_size", "value": 42, "asked": []})
    assert r.status_code == 422


# --------------------------------------------------------------------------- the questions


def test_at_most_three_questions_and_foundations_are_confirmed_not_assumed(engine, client):
    state, n = _walk(client, GLOBAL_BROKER)
    assert 1 <= n <= solon.MAX_QUESTIONS
    assert state["validation"]["valid"] is True, state["validation"]["errors"]
    held = set(state["attributes"]["authorisations"])
    # the validity rules' foundations were offered as a follow-up and accepted, never silently added
    assert {"UK MiFID investment firm (Part 4A permission)", "FINRA member firm"} <= held
    assert state["attributes"]["financial_entity_dora"] is True  # EU investment firm → DORA flag derived by L4


def test_foundations_question_carries_the_rule_that_demands_it(engine, client):
    state = client.post("/solon/intake", json={"text": "A UK retail broker holding client money."}).json()
    step = client.post("/solon/answer", json={"attributes": state["attributes"], "attribute": "authorisations",
                                               "value": ["Dealing in investments as agent", "Client money permission (CASS)"],
                                               "asked": []}).json()
    assert step["complete"] is False and step["kind"] == "foundations" and step["merge"] is True
    assert step["suggested"] == ["UK MiFID investment firm (Part 4A permission)"]
    assert "Part 4A" in step["why"]
    assert {o["label"] for o in step["options"]} == {"UK MiFID investment firm (Part 4A permission)"}


def test_direct_next_question_when_profile_already_valid(engine):
    with engine.connect() as conn:
        onto = Ontology(conn)
    step = solon.next_question(onto, BROKER, asked=[])
    assert step["complete"] is True and step["validation"]["valid"] is True


# --------------------------------------------------------------------------- the build


def test_build_narrates_every_layer_inside_the_budget(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    state, _ = _walk(client, "A UK retail equities broker holding client money for retail and professional clients.")
    t0 = time.perf_counter()
    r = client.post("/solon/build", json={"attributes": state["attributes"], "name": "front door test"})
    wall = time.perf_counter() - t0
    assert r.status_code == 200, r.text
    out = r.json()
    assert out["ok"] and out["within_budget"] and out["total_ms"] < solon.BUDGET_MS and wall < 60
    layers = [s["layer"] for s in out["steps"]]
    assert layers == ["L4", "L1", "L2", "L5", "L3", "L6", "done"]  # narrative order: who → what binds → how → programme
    by = {s["layer"]: s for s in out["steps"]}
    assert by["L1"]["detail"]["count"] >= 1 and by["L1"]["detail"]["sources"][0]["key"]
    assert by["L2"]["detail"]["count"] >= 1 and by["L2"]["detail"]["sample"][0]["id"]
    assert by["L6"]["detail"]["coverage_summary"]["total"] >= 1
    assert by["L6"]["detail"]["blueprint_id"].startswith("BLU-")
    assert out["blueprint_id"] == by["L6"]["detail"]["blueprint_id"] and out["profile_id"].startswith("PRF-")
    for s in out["steps"]:
        assert "ms" in s and s["ms"] >= 0 and s["title"]
    # evidence chain one click away
    node = client.get(f"/explore/node/{out['blueprint_id']}").json()
    assert node["kind"] == "blueprint" and node["why"] and node["history"]
    assert client.get(f"/l6/blueprints/{out['blueprint_id']}/export", params={"format": "oscal"}).status_code == 200


def test_build_streams_one_event_per_layer(engine, client):
    pid = client.post("/solon/profile", json={"attributes": BROKER, "name": "sse"}).json()["profile_id"]
    with client.stream("GET", "/solon/build/stream", params={"profile_id": pid}) as r:
        assert r.status_code == 200 and r.headers["content-type"].startswith("text/event-stream")
        body = "".join(r.iter_text())
    assert body.count("event: step") == 7  # six layers + done
    assert body.rstrip().endswith("event: end\ndata: {}")
    assert '"layer": "L4"' in body and '"layer": "done"' in body


def test_build_refuses_invalid_profiles_honestly(engine, client):
    r = client.post("/solon/build", json={"attributes": {"jurisdictions": ["UK"], "products": ["client money holding"]}})
    assert r.status_code == 422
    steps = r.json()["detail"]["steps"]
    assert steps[0]["layer"] == "L4" and steps[0]["status"] == "error" and steps[0]["detail"][0]["code"]
    assert client.post("/solon/build", json={"profile_id": "PRF-999999"}).status_code == 404
    assert client.post("/solon/build", json={}).status_code == 422
    assert client.post("/solon/profile", json={"attributes": {"jurisdictions": ["UK"], "products": ["client money holding"]}}).status_code == 422


# --------------------------------------------------------------------------- design rules (§5)


def test_no_paywall(engine, client):
    """I9 — open by mode: the whole describe → blueprint path needs no account, key or payment."""
    home = client.get("/")
    assert home.status_code == 200
    text = home.text
    assert "No account, no key, no paywall" in text
    assert "Describe your organization" in text
    # a fresh client with no cookies or headers walks the whole path
    client.cookies.clear()
    state, _ = _walk(client, GLOBAL_BROKER)
    r = client.post("/solon/build", json={"attributes": state["attributes"]})
    assert r.status_code == 200 and r.json()["blueprint_id"]
    bid = r.json()["blueprint_id"]
    for path in (f"/l6/blueprints/{bid}", f"/l6/blueprints/{bid}/export?format=oscal", f"/explore/node/{bid}",
                 "/watch/feed", "/evals/summary", "/solon/examples"):
        assert client.get(path).status_code == 200, path
    # the pages themselves are open too
    for page in ("/", "/explore", "/map", "/learn", "/watch", "/build", "/evals", "/stack"):
        r = client.get(page)
        assert r.status_code == 200 and "Cache-Control" in r.headers, page
    assert client.get(f"/graph/subgraph?focus={bid}&depth=2").status_code == 200


def test_solon_hands_the_finished_blueprint_to_the_map(engine, client):
    """The front door's "Explore the evidence chain" opens the program as a graph, and
    the L6 browser has the same door beside Priorities."""
    home = client.get("/").text
    assert '"/map#" + done.detail.blueprint_id}>Explore the evidence chain' in home
    assert 'href="/map">Map</a>' in home
    l6 = client.get("/l6").text
    assert '"/map#" + bp.blueprint_id}>Map</a>' in l6


def test_front_door_pages_meet_the_static_wcag_rules():
    import subprocess
    import sys
    from pathlib import Path

    root = Path(__file__).resolve().parents[1]
    r = subprocess.run([sys.executable, str(root / "scripts" / "a11y_check.py")], capture_output=True, text=True, cwd=root)
    assert r.returncode == 0, r.stdout + r.stderr


def test_examples_and_progress_narrative_contract(engine, client):
    ex = client.get("/solon/examples").json()
    assert ex["max_questions"] == 3 and ex["budget_ms"] == 60_000
    assert any(e["id"] == "global-retail-broker" for e in ex["examples"])
    # every example reads at least one fact (the front door never starts from nothing on its own examples)
    for e in ex["examples"]:
        body = client.post("/solon/intake", json={"text": e["text"]}).json()
        assert body["attributes"].get("jurisdictions"), e["id"]

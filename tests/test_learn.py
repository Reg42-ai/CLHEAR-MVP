"""HLD v2 §5 — Learn: tours, obligation of the week, playgrounds, learning
path with a badge, quizzes. Lessons read the layers; nothing is invented."""
from __future__ import annotations

from datetime import date, timedelta

from app.clhear import learn
from tests.test_l6_blueprints import BROKER, _corpus


def test_tours_cover_the_eight_layers_and_link_into_them(client):
    tours = client.get("/learn/tours").json()["tours"]
    assert {t["id"] for t in tours} >= {"tour-eight-layers", "tour-from-text-to-obligation", "tour-your-blueprint"}
    eight = client.get("/learn/tours/tour-eight-layers").json()
    hrefs = [s["href"] for s in eight["steps"]]
    assert hrefs[:6] == ["/l1", "/l2", "/l3", "/l4", "/l5", "/l6"]
    assert all(s["title"] and s["body"] for s in eight["steps"])
    assert client.get("/learn/tours/nope").status_code == 404


def test_obligation_of_the_week_is_deterministic_and_reads_the_registry(engine, client, tmp_path):
    assert client.get("/learn/obligation-of-the-week").json()["obligation"] is None  # honest on an empty registry
    _corpus(engine, tmp_path)
    a = client.get("/learn/obligation-of-the-week", params={"day": "2026-03-04"}).json()
    b = client.get("/learn/obligation-of-the-week", params={"day": "2026-03-06"}).json()  # same ISO week
    assert a["week"] == b["week"] == "2026-W10"
    assert a["obligation"]["obligation_id"] == b["obligation"]["obligation_id"]
    ob = a["obligation"]
    assert ob["statement"] and ob["source_key"] == "synthetic/uk-composer" and ob["jurisdiction"] == "UK"
    assert ob["hrefs"]["explore"].startswith("/explore#") and ob["hrefs"]["source"] == "/l1#synthetic/uk-composer"
    assert len(ob["read_it_this_way"]) == 4
    # a different week rotates through the registry (five obligations → picks differ somewhere in the year)
    picks = {client.get("/learn/obligation-of-the-week", params={"day": (date(2026, 1, 5) + timedelta(weeks=i)).isoformat()}).json()["obligation"]["obligation_id"]
             for i in range(8)}
    assert len(picks) > 1


def test_predicate_playground_runs_the_real_l4_predicates(engine, client, tmp_path):
    _corpus(engine, tmp_path)
    before = {**BROKER, "products": ["equities brokerage"], "authorisations": ["UK MiFID investment firm (Part 4A permission)", "Dealing in investments as agent"]}
    out = client.post("/learn/playground/predicates", json={"attributes": before, "compare_with": BROKER}).json()
    assert out["valid"] is True
    assert out["count"] >= 1 and out["by_jurisdiction"].get("UK") == out["count"]
    # adding client money holding (and its CASS permission) brings the client-money duties in
    assert out["starts_binding"] and all(o["obligation_id"] for o in out["starts_binding"])
    assert out["stops_binding"] == []
    invalid = client.post("/learn/playground/predicates", json={"attributes": {"jurisdictions": ["UK"], "products": ["client money holding"]}}).json()
    assert invalid["valid"] is False and invalid["errors"]


def test_quiz_grading_explains_each_answer_and_only_a_pass_advances_the_path(client):
    quiz = client.get("/learn/quizzes/quiz-invariants").json()
    assert quiz["pass_mark"] == 0.7 and all("answer" not in q for q in quiz["questions"])  # answers never leak
    headers = {"X-Learner-Id": "learner-1"}
    wrong = client.post("/learn/quizzes/quiz-invariants/grade", json={"answers": {q["id"]: 2 for q in quiz["questions"]}}, headers=headers).json()
    assert wrong["passed"] is False and all(r["why"] for r in wrong["results"])
    assert client.get("/learn/path", headers=headers).json()["done"] == 0
    right = {"q1": 1, "q2": 1, "q3": 1, "q4": 1, "q5": 0}
    passed = client.post("/learn/quizzes/quiz-invariants/grade", json={"answers": right}, headers=headers).json()
    assert passed["passed"] is True and passed["score"] == 1.0
    assert passed["progress"]["done"] == 1
    # quiz steps cannot be ticked off without passing
    assert client.post("/learn/path/quiz-invariants/complete", json={"kind": "quiz"}, headers=headers).status_code == 422
    assert client.post("/learn/quizzes/nope/grade", json={"answers": {}}, headers=headers).status_code == 404


def test_learning_path_earns_the_badge_when_every_step_is_done(client):
    headers = {"X-Learner-Id": "learner-2"}
    anon = client.get("/learn/path").json()
    assert anon["learner"] is None and anon["badge"]["earned"] is False and anon["total"] == len(learn.PATH)
    assert client.post("/learn/path/tour-eight-layers/complete", json={"kind": "tour"}).status_code == 401  # needs an identity
    for step in learn.PATH:
        if step["kind"] == "quiz":
            quiz = next(q for q in learn.QUIZZES if q["id"] == step["id"])
            answers = {q["id"]: q["answer"] for q in quiz["questions"]}
            r = client.post(f"/learn/quizzes/{step['id']}/grade", json={"answers": answers}, headers=headers)
        else:
            r = client.post(f"/learn/path/{step['id']}/complete", json={"kind": step["kind"]}, headers=headers)
        assert r.status_code == 200, r.text
    path = client.get("/learn/path", headers=headers).json()
    assert path["done"] == path["total"] and path["badge"]["earned"] is True and path["badge"]["earned_at"]
    # idempotent: completing again keeps one row per step
    client.post("/learn/path/tour-eight-layers/complete", json={"kind": "tour"}, headers=headers)
    assert client.get("/learn/path", headers=headers).json()["done"] == path["total"]
    assert client.post("/learn/path/unknown-step/complete", json={"kind": "tour"}, headers=headers).status_code == 404


def test_learn_page_served(client):
    r = client.get("/learn")
    assert r.status_code == 200 and "Learning path" in r.text and "Obligation of the week" in r.text
    assert client.get("/learn/playgrounds").json()["playgrounds"][0]["href"] == "/"

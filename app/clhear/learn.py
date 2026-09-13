"""HLD v2 §5 "Learn": tours, obligation of the week, playgrounds, learning
path with a badge, quizzes.

Everything here reads the layers — nothing is invented for the lesson. The
obligation of the week is a deterministic pick from the live L2 registry
(same week, same obligation, on every instance with the same corpus); the
predicate playground runs the real L4 predicates; quiz answers cite the HLD
invariant they teach. Progress is recorded per learner (anonymous learner id
or signed-in user) in ``community.learning_progress``.
"""
from __future__ import annotations

import hashlib
from datetime import date, datetime, timezone
from pathlib import Path

import sqlalchemy as sa
from fastapi import APIRouter, Header, HTTPException, Query, Request
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, Field
from sqlalchemy.engine import Connection, Engine

from app.clhear.community_models import learning_progress
from app.clhear.db import get_engine
from app.clhear.derived_models import obligations
from app.clhear.l1.models import sources
from app.clhear.l4.predicates import LIVE_STATUS, obligations_for_attributes
from app.clhear.l4.validate import Ontology, validate_with

router = APIRouter(tags=["learn"])
WEB_DIR = Path(__file__).resolve().parent / "web"

PASS_MARK = 0.7

TOURS: list[dict] = [
    {
        "id": "tour-eight-layers",
        "title": "The eight layers in eight minutes",
        "summary": "Why regulation, obligations, building blocks, profiles, activities and blueprints are separate layers — and why each one only derives downward.",
        "steps": [
            {"title": "L1 — the text, verbatim", "body": "CLHEAR stores the regulation exactly as published, clause by clause, with the rights basis that says what may be shown. Nothing above this layer may quote text the rights do not permit (I8).", "href": "/l1"},
            {"title": "L2 — obligations, one per duty", "body": "Each obligation is derived from clauses (asserts edges), deduplicated across instruments, and never deleted — a revoked duty is marked stale and superseded (I2).", "href": "/l2"},
            {"title": "L3 — building blocks", "body": "Systems, documents, roles, processes: the reusable things a firm builds once and points many obligations at. Each block says which obligation selectors it satisfies.", "href": "/l3"},
            {"title": "L4 — who you are", "body": "Jurisdictions, authorisations, products, clients. Predicates on these attributes decide which obligations bind you; validity rules stop impossible profiles (an RAO permission without Part 4A).", "href": "/l4"},
            {"title": "L5 — what you do", "body": "Business activities imply compliance activities; compliance activities operate blocks and mitigate obligations. This is the layer that turns 'must' into 'someone does this, on that system'.", "href": "/l5"},
            {"title": "L6 — your blueprint", "body": "A minimal set of blocks and activities that covers every binding obligation, with a proof of which items are load-bearing and a why-trail for every choice.", "href": "/l6"},
            {"title": "Every write carries a why", "body": "Each row in every layer points at a why-trail: the evidence, the model manifest, the confidence (I3). That is what 'evidence one click away' means on every page here.", "href": "/explore"},
        ],
    },
    {
        "id": "tour-from-text-to-obligation",
        "title": "From text to obligation",
        "summary": "Follow one clause through detection, extraction, review and change.",
        "steps": [
            {"title": "A version arrives", "body": "An adapter fetches the instrument, hashes each clause and records a change event with the effective date it found in the text or from the publisher — detection date and effective date are different facts (I7).", "href": "/l1"},
            {"title": "The extractor reads the clause", "body": "Clauses with normative language become candidate obligations with subject, action, condition and object. Low-confidence candidates queue for a human; nothing publishes below the gate (I10).", "href": "/l2"},
            {"title": "History never disappears", "body": "When the clause changes, the obligation is re-derived; the old reading is invalidated with a why-trail and the change appears on the public feed with its effective date.", "href": "/watch"},
        ],
    },
    {
        "id": "tour-your-blueprint",
        "title": "Describe your organisation, get a blueprint",
        "summary": "What Solon asks, what it never assumes, and how to read the result.",
        "steps": [
            {"title": "One field", "body": "Describe the organisation in your own words. Solon reads only what the ontology can resolve — a product never implies a licence; it asks.", "href": "/"},
            {"title": "At most three questions", "body": "L4 asks for what it is missing and offers the permissions that would permit your products in your jurisdictions. Validity rules confirm the foundations those permissions need.", "href": "/"},
            {"title": "A narrated build", "body": "Each layer reports what it found, in order, inside the budget. The blueprint links to its why-trails, its OSCAL export and the profiles that share its blocks.", "href": "/l6"},
        ],
    },
]

PLAYGROUNDS: list[dict] = [
    {"id": "solon", "title": "Solon front door", "what": "Describe an organisation and watch the layers build a blueprint.", "href": "/"},
    {"id": "predicates", "title": "Predicate playground", "what": "Change one L4 attribute and see which obligations start or stop binding.", "href": "/learn#predicates", "api": "POST /learn/playground/predicates"},
    {"id": "compare", "title": "Cross-jurisdiction compare", "what": "Same theme, two jurisdictions, one table.", "href": "/explore#compare"},
    {"id": "constellation", "title": "Blueprint constellation", "what": "Any node with its one-hop neighbourhood, why and history.", "href": "/explore"},
]

QUIZZES: list[dict] = [
    {
        "id": "quiz-invariants",
        "title": "The invariants",
        "questions": [
            {"id": "q1", "prompt": "An obligation is repealed. What happens to its row?",
             "options": ["It is deleted", "It is marked stale and superseded, never deleted", "It is moved to an archive table"],
             "answer": 1, "why": "I2 — never delete; invalidate and supersede so history stays addressable."},
            {"id": "q2", "prompt": "Solon reads 'we hold client money'. Which authorisation does it store?",
             "options": ["The CASS permission, because client money implies it", "None — it offers the permissions that would permit the product and asks", "Part 4A, because every UK firm has it"],
             "answer": 1, "why": "L4 is closed-world: a product never implies a licence; the builder asks and validity rules confirm foundations."},
            {"id": "q3", "prompt": "May an L4 predicate cite an L6 blueprint as its evidence?",
             "options": ["Yes, evidence can come from anywhere", "No — layers derive downward only", "Only with maintainer approval"],
             "answer": 1, "why": "I1 — every layer derives only from the layers below it."},
            {"id": "q4", "prompt": "A layer's eval gate failed this release. What gets published?",
             "options": ["Everything, with a warning", "Nothing from that layer or above", "Only the rows that passed"],
             "answer": 1, "why": "I10 — evals gate publication; layers above a failed gate are frozen."},
            {"id": "q5", "prompt": "Which fact does the change feed show separately from the detection date?",
             "options": ["The effective date and where it came from", "The number of watchers", "The model that detected it"],
             "answer": 0, "why": "I7 — change date and detection date are distinct; the basis (text / publisher / none) is shown."},
        ],
    },
    {
        "id": "quiz-reading-a-blueprint",
        "title": "Reading a blueprint",
        "questions": [
            {"id": "q1", "prompt": "What does 'load-bearing' mean for a blueprint item?",
             "options": ["It is the most expensive block", "Removing it would leave at least one obligation uncovered", "It was chosen by a human"],
             "answer": 1, "why": "The minimality proof lists, per item, the obligations only it satisfies."},
            {"id": "q2", "prompt": "A blueprint shows a 'gap'. What is it?",
             "options": ["A binding obligation no block in L3 satisfies yet", "An obligation with low confidence", "A product with no licence"],
             "answer": 0, "why": "Gaps are honest: the composer reports obligations it cannot cover instead of hiding them."},
            {"id": "q3", "prompt": "Who may read the agnostic blueprint?",
             "options": ["Members only", "Anyone — no account, no key, no paywall", "API-key holders"],
             "answer": 1, "why": "I9 — open by mode, not by layer: the agnostic blueprint is public."},
        ],
    },
]

# The learning path: finish every step to earn the badge.
PATH: list[dict] = [
    {"id": "tour-eight-layers", "kind": "tour", "title": "Take the eight-layer tour"},
    {"id": "tour-from-text-to-obligation", "kind": "tour", "title": "Follow a clause to an obligation"},
    {"id": "playground:predicates", "kind": "playground", "title": "Try the predicate playground"},
    {"id": "quiz-invariants", "kind": "quiz", "title": "Pass the invariants quiz"},
    {"id": "tour-your-blueprint", "kind": "tour", "title": "Build a blueprint with Solon"},
    {"id": "quiz-reading-a-blueprint", "kind": "quiz", "title": "Pass the blueprint quiz"},
]
BADGE = {"id": "clhear-reader", "title": "CLHEAR Reader", "description": "Completed the CLHEAR learning path: eight layers, one clause end to end, the invariants."}


def _iso(value) -> str | None:
    return value.isoformat() if isinstance(value, (datetime, date)) else (str(value) if value else None)


# --------------------------------------------------------------------------- obligation of the week


def week_key(day: date | None = None) -> str:
    day = day or datetime.now(timezone.utc).date()
    year, week, _ = day.isocalendar()
    return f"{year}-W{week:02d}"


def obligation_of_the_week(conn: Connection, day: date | None = None) -> dict | None:
    """A deterministic pick from the live registry for the ISO week: the same
    corpus shows the same obligation everywhere; the pick rotates weekly."""
    key = week_key(day)
    ids = [r[0] for r in conn.execute(
        sa.select(obligations.c.id).where(obligations.c.status.in_(LIVE_STATUS)).where(obligations.c.canonical_id.is_(None))
        .where(obligations.c.statement != "").order_by(obligations.c.id))]
    if not ids:
        return None
    idx = int(hashlib.sha256(key.encode()).hexdigest(), 16) % len(ids)
    ob = conn.execute(sa.select(obligations).where(obligations.c.id == ids[idx])).mappings().one()
    src = conn.execute(sa.select(sources.c.name, sources.c.short_name, sources.c.canonical_url, sources.c.rights_basis)
                       .where(sources.c.key == ob["source_key"])).mappings().first()
    return {
        "week": key, "obligation_id": ob["stable_id"] or ob["id"], "derivation_key": ob["id"], "title": ob["title"],
        "statement": ob["statement"], "determination": ob["determination"], "subject": ob["subject"], "action": ob["action"],
        "condition": ob["condition"], "jurisdiction": ob["jurisdiction"], "regulator": ob["regulator"],
        "obligation_type": ob["obligation_type"], "modality": ob["modality"], "confidence": float(ob["confidence"] or 0),
        "status": ob["status"], "clause_ref": ob["clause_ref"], "source_key": ob["source_key"],
        "source": dict(src) if src else {"name": ob["source_key"]},
        "hrefs": {"obligation": f"/l2#{ob['id']}", "explore": f"/explore#{ob['stable_id'] or ob['id']}", "source": f"/l1#{ob['source_key']}"},
        "read_it_this_way": [
            f"Who: {ob['subject'] or ob['addressee'] or 'the addressee named in the clause'}",
            f"Must: {ob['action'] or ob['determination'] or ob['title']}",
            f"When: {ob['condition'] or 'always, while the instrument is in force'}",
            "Why we say so: open the obligation and follow its why-trail to the clause.",
        ],
    }


# --------------------------------------------------------------------------- playgrounds


def predicate_playground(engine: Engine, attributes: dict, *, compare_with: dict | None = None) -> dict:
    """Which live obligations bind ``attributes`` — and, when ``compare_with``
    is given, which start or stop binding after the change."""
    with engine.connect() as conn:
        onto = Ontology(conn)
        base = obligations_for_attributes(conn, attributes)
        other = obligations_for_attributes(conn, compare_with) if compare_with is not None else None
        validity = validate_with(onto, attributes)
    out = {
        "attributes": attributes, "valid": validity["valid"], "errors": validity["errors"],
        "count": len(base), "obligations": base[:100],
        "by_jurisdiction": _count(base, "jurisdiction"), "by_type": _count(base, "obligation_type"),
    }
    if other is not None:
        a = {o["obligation_id"]: o for o in base}
        b = {o["obligation_id"]: o for o in other}
        out["compare_with"] = compare_with
        out["starts_binding"] = [b[k] for k in sorted(set(b) - set(a))]
        out["stops_binding"] = [a[k] for k in sorted(set(a) - set(b))]
    return out


def _count(rows: list[dict], key: str) -> dict[str, int]:
    out: dict[str, int] = {}
    for r in rows:
        out[r.get(key) or "unknown"] = out.get(r.get(key) or "unknown", 0) + 1
    return dict(sorted(out.items()))


# --------------------------------------------------------------------------- quizzes


def quiz_public(quiz: dict) -> dict:
    return {"id": quiz["id"], "title": quiz["title"], "pass_mark": PASS_MARK,
            "questions": [{"id": q["id"], "prompt": q["prompt"], "options": q["options"]} for q in quiz["questions"]]}


def grade(quiz_id: str, answers: dict[str, int]) -> dict:
    quiz = next((q for q in QUIZZES if q["id"] == quiz_id), None)
    if quiz is None:
        raise LookupError(quiz_id)
    results = []
    correct = 0
    for q in quiz["questions"]:
        given = answers.get(q["id"])
        ok = given is not None and int(given) == q["answer"]
        correct += ok
        results.append({"id": q["id"], "correct": ok, "answer": q["answer"], "given": given, "why": q["why"]})
    score = correct / len(quiz["questions"]) if quiz["questions"] else 0.0
    return {"quiz_id": quiz_id, "score": round(score, 3), "passed": score >= PASS_MARK, "pass_mark": PASS_MARK,
            "correct": correct, "total": len(quiz["questions"]), "results": results}


# --------------------------------------------------------------------------- progress / path


def learner_id(request: Request, x_learner_id: str | None) -> str:
    from app.clhear.accounts import current_user

    user = current_user(request)
    if user:
        return f"user:{user['id']}"
    if x_learner_id and x_learner_id.strip():
        return f"anon:{x_learner_id.strip()[:64]}"
    raise HTTPException(status_code=401, detail="Sign in or send X-Learner-Id to track progress")


def complete_step(engine: Engine, learner: str, step_id: str, *, kind: str, score: float | None = None, detail: dict | None = None) -> dict:
    if step_id not in {s["id"] for s in PATH}:
        raise LookupError(step_id)
    with engine.begin() as conn:
        existing = conn.execute(
            sa.select(learning_progress.c.id).where(learning_progress.c.learner_id == learner).where(learning_progress.c.step_id == step_id)
        ).first()
        values = {"kind": kind, "score": score, "detail": detail or {}, "completed_at": datetime.now(timezone.utc)}
        if existing:
            conn.execute(learning_progress.update().where(learning_progress.c.id == existing[0]).values(**values))
        else:
            conn.execute(learning_progress.insert().values(learner_id=learner, step_id=step_id, **values))
    return progress(engine, learner)


def progress(engine: Engine, learner: str) -> dict:
    with engine.connect() as conn:
        rows = {r["step_id"]: r for r in conn.execute(
            sa.select(learning_progress).where(learning_progress.c.learner_id == learner)).mappings()}
    steps = []
    for s in PATH:
        r = rows.get(s["id"])
        steps.append({**s, "done": r is not None, "completed_at": _iso(r["completed_at"]) if r else None,
                      "score": float(r["score"]) if r and r["score"] is not None else None})
    done = sum(1 for s in steps if s["done"])
    earned = done == len(PATH)
    return {"learner": learner, "steps": steps, "done": done, "total": len(PATH),
            "badge": {**BADGE, "earned": earned, "earned_at": max((s["completed_at"] for s in steps if s["done"]), default=None) if earned else None}}


# --------------------------------------------------------------------------- routes


class PlaygroundBody(BaseModel):
    attributes: dict = Field(default_factory=dict)
    compare_with: dict | None = None


class GradeBody(BaseModel):
    answers: dict[str, int] = Field(default_factory=dict)


class CompleteBody(BaseModel):
    kind: str = "tour"
    detail: dict = Field(default_factory=dict)


@router.get("/learn", response_class=HTMLResponse, include_in_schema=False)
def learn_page() -> HTMLResponse:
    return HTMLResponse((WEB_DIR / "learn.html").read_text(), headers={"Cache-Control": "no-cache, must-revalidate"})


@router.get("/learn/tours")
def tours() -> dict:
    return {"tours": [{"id": t["id"], "title": t["title"], "summary": t["summary"], "steps": len(t["steps"])} for t in TOURS]}


@router.get("/learn/tours/{tour_id}")
def tour(tour_id: str) -> dict:
    t = next((t for t in TOURS if t["id"] == tour_id), None)
    if t is None:
        raise HTTPException(status_code=404, detail=f"unknown tour {tour_id}")
    return t


@router.get("/learn/obligation-of-the-week")
def weekly(day: date | None = Query(default=None)) -> dict:
    with get_engine().connect() as conn:
        pick = obligation_of_the_week(conn, day)
    return {"week": week_key(day), "obligation": pick,
            "empty_reason": None if pick else "the registry has no live obligations with public text yet"}


@router.get("/learn/playgrounds")
def playgrounds() -> dict:
    return {"playgrounds": PLAYGROUNDS}


@router.post("/learn/playground/predicates")
def playground_predicates(body: PlaygroundBody) -> dict:
    return predicate_playground(get_engine(), body.attributes, compare_with=body.compare_with)


@router.get("/learn/quizzes")
def quizzes() -> dict:
    return {"quizzes": [quiz_public(q) for q in QUIZZES]}


@router.get("/learn/quizzes/{quiz_id}")
def quiz(quiz_id: str) -> dict:
    q = next((q for q in QUIZZES if q["id"] == quiz_id), None)
    if q is None:
        raise HTTPException(status_code=404, detail=f"unknown quiz {quiz_id}")
    return quiz_public(q)


@router.post("/learn/quizzes/{quiz_id}/grade")
def grade_quiz(quiz_id: str, body: GradeBody, request: Request,
               x_learner_id: str | None = Header(default=None, alias="X-Learner-Id")) -> dict:
    try:
        result = grade(quiz_id, body.answers)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"unknown quiz {quiz_id}")
    if result["passed"]:
        try:
            learner = learner_id(request, x_learner_id)
        except HTTPException:
            learner = None
        if learner:
            result["progress"] = complete_step(get_engine(), learner, quiz_id, kind="quiz", score=result["score"],
                                               detail={"correct": result["correct"], "total": result["total"]})
    return result


@router.get("/learn/path")
def learning_path(request: Request, x_learner_id: str | None = Header(default=None, alias="X-Learner-Id")) -> dict:
    try:
        learner = learner_id(request, x_learner_id)
    except HTTPException:
        return {"learner": None, "steps": [{**s, "done": False} for s in PATH], "done": 0, "total": len(PATH),
                "badge": {**BADGE, "earned": False}}
    return progress(get_engine(), learner)


@router.post("/learn/path/{step_id}/complete")
def complete(step_id: str, body: CompleteBody, request: Request,
             x_learner_id: str | None = Header(default=None, alias="X-Learner-Id")) -> dict:
    learner = learner_id(request, x_learner_id)
    if body.kind == "quiz":
        raise HTTPException(status_code=422, detail="quiz steps are completed by passing the quiz")
    try:
        return complete_step(get_engine(), learner, step_id, kind=body.kind, detail=body.detail)
    except LookupError:
        raise HTTPException(status_code=404, detail=f"unknown step {step_id}")

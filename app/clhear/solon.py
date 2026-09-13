"""Solon front door (HLD v2 §5): "Describe your organization" -> the two or
three questions L4 still needs -> a validated profile -> a blueprint in under
sixty seconds, narrated layer by layer instead of a spinner.

Closed world throughout: the description is read against the L4 ontology
(jurisdictions, licences, products, client types); nothing that does not
resolve to an ontology row becomes a profile fact, and authorisations are
never assumed from products — they are *asked*, with the licences that would
permit the described products offered as options. An optional router pass
(``solon.intake``) may read the free text too; its answers go through the
same resolver and are discarded when they do not resolve.
"""
from __future__ import annotations

import json
import logging
import re
import time
from collections.abc import Iterator
from datetime import datetime, timezone

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.l4 import builder as l4_builder
from app.clhear.l4 import validate as l4_validate
from app.clhear.l4.validate import Ontology

log = logging.getLogger("clhear.solon")

AGENT = "solon.intake"
BUDGET_MS = 60_000

# Cue tables: phrase -> ontology value. Values are ontology names (or codes);
# they are still resolved through the Lookup so a stale cue drops out instead
# of inventing a fact.
JURISDICTION_CUES = {
    "UK": ["uk", "u.k.", "united kingdom", "britain", "british", "england", "london", "fca", "pra", "fca-authorised", "fca authorised"],
    "EU": ["eu", "e.u.", "europe", "european", "eea", "esma", "eba", "mifid", "mica", "psd2", "emd2", "dora", "ireland", "germany",
           "france", "netherlands", "luxembourg", "cyprus", "lithuania", "malta"],
    "US": ["us", "u.s.", "usa", "united states", "america", "american", "sec", "finra", "fincen", "cftc", "nfa", "new york",
           "delaware", "broker-dealer", "broker dealer", "ria"],
}
PRODUCT_CUES = {
    "equities brokerage": ["equities brokerage", "equity brokerage", "stock broker", "stockbroker", "stock brokerage", "brokerage",
                           "broker", "equities", "shares", "stocks", "execution"],
    "CFDs and leveraged derivatives": ["cfd", "cfds", "contracts for difference", "leveraged", "spread bet", "spread betting", "forex", "fx trading"],
    "listed options and futures": ["options", "futures", "listed derivatives"],
    "investment advice": ["advice", "advisory", "adviser", "advisor", "financial planning"],
    "portfolio management": ["portfolio management", "asset management", "wealth management", "discretionary", "fund management",
                             "managed portfolios"],
    "custody of client assets": ["custody", "custodian", "safekeeping", "safeguarding assets"],
    "client money holding": ["client money", "client funds", "customer money", "hold money for clients", "holds client money"],
    "margin lending": ["margin", "margin lending", "leverage lending"],
    "securities lending": ["securities lending", "stock lending", "repo"],
    "investment research": ["research", "analyst reports"],
    "e-money issuance": ["e-money", "emoney", "electronic money", "emi", "prepaid", "stored value", "digital wallet", "e-wallet"],
    "payment accounts": ["payment accounts", "payment account", "payments", "payment services", "money transfer", "remittance",
                         "payment institution", "psp"],
    "card programmes": ["card", "cards", "debit card", "prepaid card", "card programme", "card program", "card issuing"],
    "deposit taking": ["deposits", "deposit taking", "bank", "banking", "savings accounts", "current accounts"],
    "crypto custody": ["crypto custody", "custody of crypto", "digital asset custody", "wallet provider"],
    "crypto exchange": ["crypto exchange", "cryptocurrency exchange", "exchange crypto", "crypto trading", "crypto-asset exchange",
                        "crypto", "cryptocurrency", "digital assets", "bitcoin", "stablecoin", "casp"],
    "crypto brokerage": ["crypto broker", "crypto brokerage", "buy and sell crypto"],
}
CLIENT_CUES = {
    "retail": ["retail", "consumers", "consumer", "individuals", "private clients", "the public", "everyday", "mass market", "b2c"],
    "professional": ["professional clients", "professional", "sophisticated", "hnw", "high net worth", "family offices"],
    "eligible counterparty": ["eligible counterparty", "eligible counterparties", "banks and brokers", "interbank"],
    "SME": ["sme", "smes", "small business", "small businesses", "merchants", "startups", "b2b"],
    "institutional": ["institutional", "institutions", "pension funds", "asset managers", "hedge funds", "corporates"],
    "accredited investor": ["accredited", "accredited investors", "qualified purchasers"],
}
DATA_CUES = {
    "large-scale personal data": ["millions of", "large-scale", "large scale", "big data", "profiling", "behavioural data"],
}
SIZE_WORDS = {"global": ["global", "worldwide", "international", "multinational", "cross-border"]}

# The questions Solon asks (everything else is derived or optional).
ASK_ORDER = ("jurisdictions", "authorisations", "products", "customer_base")
MAX_QUESTIONS = 3


def _fold(text: str) -> str:
    return re.sub(r"[^a-z0-9 ]+", " ", (text or "").lower())


def _has(folded: str, cue: str) -> bool:
    cue = _fold(cue).strip()
    return bool(cue) and re.search(rf"(?<![a-z0-9]){re.escape(cue)}(?![a-z0-9])", folded) is not None


def _resolve_list(lookup, values: list[str]) -> list[str]:
    out: list[str] = []
    for v in values:
        row = lookup.resolve(v)
        if row and row["name"] not in out:
            out.append(row["name"])
    return out


def parse_description(onto: Ontology, text: str) -> dict:
    """Read the free text against the ontology; returns ``attributes`` (only
    resolved values), ``evidence`` (which phrase produced which fact) and
    ``unresolved`` hints for the questions."""
    folded = " " + _fold(text) + " "
    evidence: list[dict] = []
    attrs: dict = {}

    jurs = []
    for code, cues in JURISDICTION_CUES.items():
        hit = next((c for c in cues if _has(folded, c)), None)
        if hit and code in onto.jurisdictions:
            jurs.append(code)
            evidence.append({"attribute": "jurisdictions", "value": code, "cue": hit})
    if jurs:
        attrs["jurisdictions"] = jurs

    products = []
    for name, cues in PRODUCT_CUES.items():
        hit = next((c for c in cues if _has(folded, c)), None)
        if hit:
            row = onto.products.resolve(name)
            if row and row["name"] not in products:
                products.append(row["name"])
                evidence.append({"attribute": "products", "value": row["name"], "cue": hit})
    if products:
        attrs["products"] = products

    clients = []
    for name, cues in CLIENT_CUES.items():
        hit = next((c for c in cues if _has(folded, c)), None)
        if hit:
            row = onto.clients.resolve(name)
            if row and row["name"] not in clients:
                clients.append(row["name"])
                evidence.append({"attribute": "customer_base", "value": row["name"], "cue": hit})
    if clients:
        attrs["customer_base"] = clients

    # Licences named outright (names or aliases) count; regime words alone do not.
    auths = []
    for lic in sorted(onto.licences.rows.values(), key=lambda r: -len(r["name"])):
        if jurs and lic["jurisdiction"].upper() not in jurs:
            continue
        names = [lic["name"], *(lic.get("aliases") or [])]
        hit = next((n for n in names if len(_fold(n).strip()) >= 4 and _has(folded, n)), None)
        if hit and lic["name"] not in auths:
            auths.append(lic["name"])
            evidence.append({"attribute": "authorisations", "value": lic["name"], "cue": hit})
    if auths:
        attrs["authorisations"] = auths

    for value, cues in DATA_CUES.items():
        hit = next((c for c in cues if _has(folded, c)), None)
        if hit:
            attrs["data_footprint"] = value
            evidence.append({"attribute": "data_footprint", "value": value, "cue": hit})
    if any(_has(folded, c) for c in ("crypto", "cryptocurrency", "digital assets", "casp", "bitcoin", "stablecoin")):
        attrs["crypto_services"] = True
        evidence.append({"attribute": "crypto_services", "value": True, "cue": "crypto"})

    hints = {"global": any(_has(folded, w) for w in SIZE_WORDS["global"])}
    return {"attributes": attrs, "evidence": evidence, "hints": hints}


def _merge_router_read(onto: Ontology, attrs: dict, read: dict, evidence: list[dict]) -> dict:
    """Router-proposed attributes are accepted only when they resolve (closed world)."""
    out = dict(attrs)
    lookups = {"authorisations": onto.licences, "products": onto.products, "customer_base": onto.clients}
    for key, lookup in lookups.items():
        proposed = read.get(key)
        if not isinstance(proposed, list):
            continue
        resolved = _resolve_list(lookup, [str(v) for v in proposed])
        for v in resolved:
            if v not in out.get(key, []):
                out.setdefault(key, []).append(v)
                evidence.append({"attribute": key, "value": v, "cue": "router", "origin": "llm"})
    if isinstance(read.get("jurisdictions"), list):
        for code in read["jurisdictions"]:
            code = str(code).upper()
            if code in onto.jurisdictions and code not in out.get("jurisdictions", []):
                out.setdefault("jurisdictions", []).append(code)
                evidence.append({"attribute": "jurisdictions", "value": code, "cue": "router", "origin": "llm"})
    return out


def intake(engine: Engine, text: str, *, llm=None) -> dict:
    """Free text -> resolved attributes + the first question."""
    with engine.connect() as conn:
        onto = Ontology(conn)
    parsed = parse_description(onto, text)
    attrs, evidence = parsed["attributes"], parsed["evidence"]
    if llm is not None and text.strip():
        try:
            from app.clhear.platform.gateway import parse_json_object
            from app.clhear.platform.router import complete

            prompt = (
                "Read this organisation description and return ONLY values from the menus. "
                'JSON: {"jurisdictions": [], "authorisations": [], "products": [], "customer_base": []}\n'
                f"Jurisdictions: {sorted(onto.jurisdictions)}\nAuthorisations: {onto.licences.names()}\n"
                f"Products: {onto.products.names()}\nClient types: {onto.clients.names()}\n\nDescription: {text.strip()}"
            )
            result = complete(llm, "solon.intake", prompt=prompt, system="Closed-world reader. JSON only. Never invent values.",
                              required_keys=["jurisdictions", "products"], max_tokens=400)
            attrs = _merge_router_read(onto, attrs, parse_json_object(result.text), evidence)
        except Exception:
            log.exception("solon intake router pass failed; deterministic read kept")
    step = next_question(onto, attrs, asked=[])
    return {"text": text, "attributes": attrs, "evidence": evidence, "hints": parsed["hints"], **step}


def _suggested_authorisations(onto: Ontology, attrs: dict) -> list[str]:
    """Licences that would permit the described products in the chosen
    jurisdictions — offered as options, never assumed."""
    jurs = {j.upper() for j in attrs.get("jurisdictions") or []}
    wanted = {onto.products.resolve(p)["id"] for p in attrs.get("products") or [] if onto.products.resolve(p)}
    out: list[str] = []
    for p in onto.permits:
        lic = onto.licences.rows.get(p["licence_id"])
        if lic is None or p["product_id"] not in wanted:
            continue
        if jurs and lic["jurisdiction"].upper() not in jurs:
            continue
        if lic["name"] not in out:
            out.append(lic["name"])
    return out


def next_question(onto: Ontology, attrs: dict, *, asked: list[str]) -> dict:
    """The next of the (at most three) questions L4 still needs; ``complete``
    when the profile validates or the budget of questions is spent."""
    attrs = l4_builder._with_flags(onto, dict(attrs))
    validation = l4_validate.validate_with(onto, attrs)
    if len(asked) < MAX_QUESTIONS:
        # Foundations the validity rules demand for what was just confirmed (an
        # RAO permission sits on Part 4A; US client money needs a carrying BD)
        # are confirmed, not assumed — one follow-up question, asked once.
        foundations = _required_foundations(onto, attrs, validation)
        if foundations and asked.count("authorisations") == 1 and attrs.get("authorisations"):
            step = l4_builder.next_step_with(onto, {k: v for k, v in attrs.items() if k != "authorisations"})
            q = _question(onto, "authorisations", step, attrs, validation, asked)
            names = set(foundations)
            q.update({
                "question": "These follow from the permissions you confirmed — which of them do you hold?",
                "kind": "foundations", "merge": True, "suggested": foundations,
                "options": [o for o in step["options"] if o["label"] in names],
                "why": "; ".join(dict.fromkeys(f["message"] for f in _rule_errors(validation))),
            })
            return q
        for key in ASK_ORDER:
            if key in asked:
                continue
            missing = attrs.get(key) in (None, [], "")
            # authorisations are asked while the profile is invalid (e.g. a product no held licence permits)
            if not missing and not (key == "authorisations" and not validation["valid"]):
                continue
            step = l4_builder.next_step_with(onto, {k: v for k, v in attrs.items() if k != key})
            if step["attribute"] != key:
                # the builder needs an earlier attribute first (e.g. jurisdictions before licences)
                if step["attribute"] in ASK_ORDER and step["attribute"] not in asked:
                    key = step["attribute"]
                else:
                    continue
            return _question(onto, key, step, attrs, validation, asked)
    return {"complete": True, "attribute": None, "question": None, "options": [], "unavailable": [], "suggested": [],
            "validation": validation, "attributes": attrs, "questions_left": MAX_QUESTIONS - len(asked)}


def _rule_errors(validation: dict) -> list[dict]:
    return [e for e in validation.get("errors", []) if e.get("code") == "validity_rule" and (e.get("requires") or {}).get("authorisations")]


def _required_foundations(onto: Ontology, attrs: dict, validation: dict) -> list[str]:
    """Authorisations the validity rules require given the held ones (in the profile's jurisdictions)."""
    held = {a for a in attrs.get("authorisations") or []}
    jurs = {j.upper() for j in attrs.get("jurisdictions") or []}
    out: list[str] = []
    for e in _rule_errors(validation):
        req = e["requires"]["authorisations"]
        for name in (req if isinstance(req, list) else [req]):
            lic = onto.licences.resolve(str(name))
            if lic is None or lic["name"] in held or (jurs and lic["jurisdiction"].upper() not in jurs):
                continue
            if lic["name"] not in out:
                out.append(lic["name"])
    return out


def _question(onto: Ontology, key: str, step: dict, attrs: dict, validation: dict, asked: list[str]) -> dict:
    suggested = _suggested_authorisations(onto, attrs) if key == "authorisations" else []
    options = step["options"]
    if suggested:
        options = sorted(options, key=lambda o: (o["label"] not in suggested, o["label"]))
    return {
        "complete": False, "attribute": key, "question": step["question"], "type": step["type"],
        "options": options, "unavailable": step.get("unavailable", []), "suggested": suggested,
        "optional": bool(step.get("optional")) and key != "authorisations",
        "why": _why_question(key, attrs), "validation": validation, "attributes": attrs,
        "questions_left": MAX_QUESTIONS - len(asked),
    }


def _why_question(key: str, attrs: dict) -> str:
    return {
        "jurisdictions": "L4 applicability predicates are jurisdiction-first: every obligation carries the jurisdiction it binds.",
        "authorisations": "Permissions decide which duties bind you. CLHEAR never assumes a licence from a product — it asks, "
                          "and offers the licences that would permit " + ", ".join(attrs.get("products") or ["your products"]) + ".",
        "products": "Products imply business activities (L5) and select the obligations conditioned on them (L4).",
        "customer_base": "Client type conditions many conduct duties (retail vs professional vs eligible counterparty).",
    }.get(key, "")


def answer(engine: Engine, attributes: dict, attribute: str, value, *, asked: list[str], merge: bool = False) -> dict:
    """Apply one answer (closed world — unresolvable values are reported, not stored)."""
    with engine.connect() as conn:
        onto = Ontology(conn)
    attrs = dict(attributes or {})
    lookups = {"authorisations": onto.licences, "products": onto.products, "customer_base": onto.clients, "channels": onto.channels}
    rejected: list[str] = []
    if attribute == "jurisdictions":
        vals = [str(v).upper() for v in (value if isinstance(value, list) else [value]) if v]
        attrs["jurisdictions"] = [v for v in vals if v in onto.jurisdictions]
        rejected = [v for v in vals if v not in onto.jurisdictions]
    elif attribute in lookups:
        vals = [str(v) for v in (value if isinstance(value, list) else [value]) if v]
        resolved = _resolve_list(lookups[attribute], vals)
        rejected = [v for v in vals if lookups[attribute].resolve(v) is None]
        if merge:
            resolved = [*(attrs.get(attribute) or []), *[r for r in resolved if r not in (attrs.get(attribute) or [])]]
        attrs[attribute] = resolved
    elif attribute in ("crypto_services", "financial_entity_dora"):
        attrs[attribute] = bool(value)
    elif attribute == "data_footprint":
        attrs[attribute] = str(value or "")
    else:
        raise ValueError(f"unknown attribute {attribute}")
    asked = [*asked, attribute]
    step = next_question(onto, attrs, asked=asked)
    return {**step, "asked": asked, "rejected": rejected}


# --------------------------------------------------------------------------- build


def _ms(t0: float) -> int:
    return int((time.perf_counter() - t0) * 1000)


def narrate(engine: Engine, attributes: dict, *, requested_by: str = "solon", profile_id: str | None = None,
            name: str = "") -> Iterator[dict]:
    """Yield one narrative step per layer as it completes (SSE / JSON)."""
    from app.clhear.l1.models import sources
    from app.clhear.l4.predicates import obligations_for_attributes
    from app.clhear.l5.map import activity_map
    from app.clhear.l6 import composer

    started = time.perf_counter()
    t = time.perf_counter()
    with engine.connect() as conn:
        onto = Ontology(conn)
    attrs = l4_builder._with_flags(onto, dict(attributes or {}))
    validation = l4_validate.validate_with(onto, attrs)
    if not validation["valid"]:
        yield {"layer": "L4", "title": "Profile does not validate", "status": "error", "ms": _ms(t),
               "detail": validation["errors"], "evidence": []}
        return
    if profile_id is None:
        prof = l4_validate.create_profile(engine, attrs, name=name or "Solon intake", source="solon")
        profile_id = prof["id"]
    licences = [onto.licences.resolve(a) for a in attrs.get("authorisations") or []]
    yield {"layer": "L4", "title": "Validated your profile against the ontology", "status": "done", "ms": _ms(t),
           "detail": {"profile_id": profile_id, "attributes": attrs, "warnings": validation["warnings"],
                      "licences": [{"id": l["id"], "name": l["name"], "register": l.get("register"), "register_url": l.get("register_url")}
                                   for l in licences if l]},
           "evidence": [f"/l4#{profile_id}"]}

    t = time.perf_counter()
    jurs = [j.upper() for j in attrs.get("jurisdictions") or []]
    with engine.connect() as conn:
        rows = conn.execute(sa.select(sources.c.key, sources.c.name, sources.c.jurisdiction)
                            .where(sa.func.upper(sources.c.jurisdiction).in_(jurs)).order_by(sources.c.key)).all()
    yield {"layer": "L1", "title": f"{len(rows)} legal sources in scope for {', '.join(jurs)}", "status": "done", "ms": _ms(t),
           "detail": {"sources": [{"key": r.key, "name": r.name, "jurisdiction": r.jurisdiction} for r in rows[:12]], "count": len(rows)},
           "evidence": ["/l1"]}

    t = time.perf_counter()
    with engine.connect() as conn:
        applicable = obligations_for_attributes(conn, attrs)
    yield {"layer": "L2", "title": f"{len(applicable)} obligations apply through L4 predicates", "status": "done", "ms": _ms(t),
           "detail": {"sample": [{"id": o["obligation_id"], "title": o["title"], "source_key": o["source_key"]} for o in applicable[:8]],
                      "count": len(applicable)},
           "evidence": [f"/l4#{profile_id}", "/l2"]}

    t = time.perf_counter()
    with engine.connect() as conn:
        amap = activity_map(conn, attrs)
    yield {"layer": "L5", "title": f"{amap['counts']['business']} business activities implied, "
                                    f"{amap['counts']['compliance']} compliance activities govern them", "status": "done", "ms": _ms(t),
           "detail": amap["counts"], "evidence": [f"/l5#{profile_id}"]}

    t = time.perf_counter()
    bp = composer.compose_for_profile(engine, profile_id, requested_by=requested_by)
    kinds = {k: len(v) for k, v in bp["program"].items()}
    yield {"layer": "L3", "title": f"{len(bp['items'])} blocks drawn from the catalogue ({', '.join(f'{n} {k}' for k, n in kinds.items())})",
           "status": "done", "ms": 0, "detail": kinds, "evidence": ["/l3"]}
    yield {"layer": "L6", "title": f"Composed the leanest complete program: {bp['coverage_summary']['covered']}/"
                                    f"{bp['coverage_summary']['total']} obligations covered, {bp['coverage_summary']['gaps']} gaps, "
                                    f"minimal={'yes' if bp['minimality']['minimal'] else 'no'}",
           "status": "done", "ms": _ms(t),
           "detail": {"blueprint_id": bp["blueprint_id"], "coverage_summary": bp["coverage_summary"], "minimality": {
               "minimal": bp["minimality"]["minimal"], "required": bp["minimality"]["required"], "selected": bp["minimality"]["selected"]},
               "engine_version": bp["engine_version"]},
           "evidence": [f"/l6#{bp['blueprint_id']}"]}
    yield {"layer": "done", "title": "Blueprint ready", "status": "done", "ms": _ms(started), "total_ms": _ms(started),
           "within_budget": _ms(started) < BUDGET_MS, "budget_ms": BUDGET_MS,
           "detail": {"blueprint_id": bp["blueprint_id"], "profile_id": profile_id}, "evidence": [f"/l6#{bp['blueprint_id']}"]}


def build(engine: Engine, attributes: dict, *, requested_by: str = "solon", profile_id: str | None = None, name: str = "") -> dict:
    """Whole narrative at once (JSON callers); the blueprint is in the last step."""
    steps = list(narrate(engine, attributes, requested_by=requested_by, profile_id=profile_id, name=name))
    last = steps[-1]
    ok = last["layer"] == "done"
    out = {"steps": steps, "ok": ok, "total_ms": last.get("total_ms", sum(s["ms"] for s in steps)),
           "within_budget": last.get("within_budget", False), "budget_ms": BUDGET_MS,
           "blueprint_id": last["detail"].get("blueprint_id") if ok else None,
           "profile_id": last["detail"].get("profile_id") if ok else profile_id,
           "generated_at": datetime.now(timezone.utc).isoformat()}
    try:
        from app.clhear import ai_ops

        ai_ops.record(engine, kind="fleet_generation", layer="L6", fleet=AGENT,
                      reasoning=f"Solon: blueprint {out['blueprint_id']} for {out['profile_id']} in {out['total_ms']} ms "
                                f"({'within' if out['within_budget'] else 'over'} the 60 s budget)" if ok else "Solon: profile did not validate",
                      detail={"profile_id": out["profile_id"], "blueprint_id": out["blueprint_id"], "total_ms": out["total_ms"]})
    except Exception:
        pass
    return out


def sse(steps: Iterator[dict]) -> Iterator[str]:
    for step in steps:
        yield f"event: step\ndata: {json.dumps(step, default=str)}\n\n"
    yield "event: end\ndata: {}\n\n"

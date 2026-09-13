"""Guided, validating profile builder (HLD v2 §4.4, §5 "profile builder that
only offers valid permutations").

The builder walks the attribute schema in dependency order — jurisdictions ->
authorisations available there -> products the held authorisations permit ->
client types -> channels -> data footprint -> regime flags — and at every
step offers only options that keep the partial profile valid against the
ontology (permits + validity rules). Options that would make an impossible
permutation are returned separately with the reason, never offered as valid.

Also the permutation explorer ("organisations like this exist in these
jurisdictions") and similar-profile lookup over stored profiles.
"""
from __future__ import annotations

import copy
import itertools

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import profiles
from app.clhear.l4.ontology import matches
from app.clhear.l4.validate import Ontology, validate_with

STEP_ORDER = ("jurisdictions", "authorisations", "products", "customer_base", "channels",
              "data_footprint", "crypto_services", "financial_entity_dora")

QUESTIONS = {
    "jurisdictions": "Where does the organisation operate or serve customers?",
    "authorisations": "Which authorisations / licences does it hold?",
    "products": "Which products and services does it offer?",
    "customer_base": "Who are its clients?",
    "channels": "How does it reach and serve customers?",
    "data_footprint": "How large is its personal-data footprint?",
    "crypto_services": "Does it provide crypto-asset services?",
    "financial_entity_dora": "Is it a DORA financial entity?",
}

DATA_FOOTPRINT_OPTIONS = ("large-scale personal data", "moderate personal data", "minimal personal data")


def _with(attrs: dict, key: str, value) -> dict:
    out = copy.deepcopy(attrs)
    spec_list = key in ("jurisdictions", "authorisations", "products", "customer_base", "channels")
    if spec_list:
        current = list(out.get(key) or [])
        if value not in current:
            current.append(value)
        out[key] = current
    else:
        out[key] = value
    return out


def _errors_for(res: dict, exclude_codes: tuple[str, ...] = ()) -> list[str]:
    return [e.get("message") or e.get("code") for e in res["errors"] if e.get("code") not in exclude_codes]


def _option(onto: Ontology, attrs: dict, key: str, row: dict | None, value, *, label: str | None = None,
            detail: str = "") -> dict:
    candidate = _with(attrs, key, value)
    res = validate_with(onto, candidate)
    # Missing foundations the user can still add later are 'requires' hints, not blockers:
    # a permutation is blocked only when adding the value contradicts what is already chosen.
    hard = [e for e in res["errors"] if e["code"] in ("authorisation_jurisdiction", "unknown_value")
            or (e["code"] == "validity_rule" and e.get("forbids"))]
    soft = [e for e in res["errors"] if e not in hard]
    return {
        "value": value, "label": label or value, "id": (row or {}).get("id"), "detail": detail,
        "valid": not hard, "blocked_by": [e.get("message") for e in hard],
        "then_requires": [e.get("message") for e in soft],
        "warnings": [w.get("message") for w in res["warnings"]],
    }


def next_step(engine: Engine, attributes: dict) -> dict:
    with engine.connect() as conn:
        onto = Ontology(conn)
    return next_step_with(onto, attributes or {})


def next_step_with(onto: Ontology, attributes: dict) -> dict:
    attrs = dict(attributes)
    current = validate_with(onto, attrs)
    jurs = [j.upper() for j in (attrs.get("jurisdictions") or [])]
    held = [onto.licences.resolve(a) for a in (attrs.get("authorisations") or [])]
    held_ids = {h["id"] for h in held if h}

    for key in STEP_ORDER:
        if key in attrs and attrs[key] not in (None, [], ""):
            continue
        step = {"attribute": key, "question": QUESTIONS[key], "type": onto.schema.get(key, {}).get("type", "list"),
                "options": [], "unavailable": [], "complete": False, "validation": current}
        if key == "jurisdictions":
            step["options"] = [_option(onto, attrs, key, None, code, label=j["name"],
                                       detail="Regulators: " + ", ".join(j.get("regulators", [])))
                               for code, j in sorted(onto.jurisdictions.items())]
            return step
        if key == "authorisations":
            for lic in sorted(onto.licences.rows.values(), key=lambda r: (r["jurisdiction"], r["name"])):
                if jurs and lic["jurisdiction"].upper() not in jurs:
                    continue
                permitted = sorted(onto.products.rows[p["product_id"]]["name"] for p in onto.permits
                                   if p["licence_id"] == lic["id"] and p["product_id"] in onto.products.rows)
                opt = _option(onto, attrs, key, lic, lic["name"],
                              detail=f"{lic['jurisdiction']} · {lic['regulator']} · {lic['regime']}")
                opt["permits"] = permitted
                opt["register"] = {"key": lic["register"], "url": lic["register_url"], "ref": lic["register_ref"], "status": lic["status"]}
                (step["options"] if opt["valid"] else step["unavailable"]).append(opt)
            step["optional"] = True
            return step
        if key == "products":
            permitted_ids = {p["product_id"] for p in onto.permits if p["licence_id"] in held_ids}
            for prod in sorted(onto.products.rows.values(), key=lambda r: r["name"]):
                by = sorted({onto.licences.rows[p["licence_id"]]["name"] for p in onto.permits
                             if p["product_id"] == prod["id"] and p["licence_id"] in onto.licences.rows
                             and (not jurs or onto.licences.rows[p["licence_id"]]["jurisdiction"].upper() in jurs)})
                opt = _option(onto, attrs, key, prod, prod["name"], detail=prod.get("description", ""))
                opt["permitted_by"] = by
                if prod["id"] in permitted_ids and opt["valid"]:
                    step["options"].append(opt)
                else:
                    opt["valid"] = False
                    opt["blocked_by"] = opt["blocked_by"] or [
                        (f"needs one of: {', '.join(by)}" if by else "no authorisation in the ontology permits this product in the chosen jurisdictions")]
                    step["unavailable"].append(opt)
            return step
        if key in ("customer_base", "channels"):
            lookup = onto.clients if key == "customer_base" else onto.channels
            for row in sorted(lookup.rows.values(), key=lambda r: r["name"]):
                opt = _option(onto, attrs, key, row, row["name"], detail=row.get("description", ""))
                (step["options"] if opt["valid"] else step["unavailable"]).append(opt)
            return step
        if key == "data_footprint":
            step["options"] = [_option(onto, attrs, key, None, v) for v in DATA_FOOTPRINT_OPTIONS]
            return step
        if key in ("crypto_services", "financial_entity_dora"):
            suggested = _suggest_flag(onto, attrs, key)
            for v in (True, False):
                opt = _option(onto, attrs, key, None, v, label="yes" if v else "no")
                opt["suggested"] = suggested is not None and v == suggested
                (step["options"] if opt["valid"] else step["unavailable"]).append(opt)
            return step
    return {"attribute": None, "question": None, "complete": True, "options": [], "unavailable": [], "validation": current}


def _suggest_flag(onto: Ontology, attrs: dict, key: str) -> bool | None:
    """What the validity rules would demand for a flag given the rest of the profile."""
    for rule in onto.rules:
        body = rule["rule"] or {}
        if rule["jurisdiction"] not in ("*", "") and rule["jurisdiction"].upper() not in [j.upper() for j in attrs.get("jurisdictions") or []]:
            continue
        if not matches(body.get("if", {}), attrs):
            continue
        req = body.get("requires") or {}
        if key in req and isinstance(req[key], bool):
            return req[key]
    return None


def _with_flags(onto: Ontology, attrs: dict) -> dict:
    """Set the regime flags the rules demand for this permutation (two passes:
    a flag can be implied by a product and in turn demand an authorisation)."""
    out = dict(attrs)
    for _ in range(2):
        for key in ("crypto_services", "financial_entity_dora"):
            s = _suggest_flag(onto, out, key)
            if s is not None:
                out[key] = s
    return out


# ----------------------------------------------------------------- permutation explorer


def closure(onto: Ontology, jurisdiction: str, authorisations: list[str]) -> list[str]:
    """Add the foundations the validity rules demand (e.g. RAO permission ->
    Part 4A) so a licence permutation is shown with what it must come with."""
    attrs = {"jurisdictions": [jurisdiction], "authorisations": list(authorisations)}
    for _ in range(6):
        added = False
        for rule in onto.rules:
            if rule["jurisdiction"] not in ("*", "", jurisdiction):
                continue
            body = rule["rule"] or {}
            req = (body.get("requires") or {}).get("authorisations")
            if req is None or not matches(body.get("if", {}), attrs) or matches({"authorisations": req}, attrs):
                continue
            # any-of foundations: the first listed is the primary one (e.g. investment firm over credit institution)
            pick = req if isinstance(req, str) else (req[0] if req else None)
            if pick and pick not in attrs["authorisations"]:
                attrs["authorisations"].append(pick)
                added = True
        if not added:
            break
    return attrs["authorisations"]


def permutations(engine: Engine, jurisdictions: list[str] | None = None, max_examples: int = 12) -> dict:
    """For each jurisdiction: every licence, the products it permits, the
    foundations it needs, and whether the minimal permutation validates.
    Pairwise licence combinations are sampled so the explorer stays bounded."""
    with engine.connect() as conn:
        onto = Ontology(conn)
    wanted = [j.upper() for j in jurisdictions] if jurisdictions else sorted(onto.jurisdictions)
    out = {"ontology_version": onto.version, "jurisdictions": []}
    for jur in wanted:
        lics = [l for l in onto.licences.rows.values() if l["jurisdiction"].upper() == jur]
        singles = []
        for lic in sorted(lics, key=lambda r: r["name"]):
            auths = closure(onto, jur, [lic["name"]])
            held = {onto.licences.resolve(a)["id"] for a in auths if onto.licences.resolve(a)}
            prods = sorted({onto.products.rows[p["product_id"]]["name"] for p in onto.permits
                            if p["licence_id"] in held and p["product_id"] in onto.products.rows})
            attrs = _with_flags(onto, {"jurisdictions": [jur], "authorisations": auths, "products": prods})
            res = validate_with(onto, attrs)
            singles.append({"licence": lic["name"], "licence_id": lic["id"], "with": [a for a in auths if a != lic["name"]],
                            "products": prods, "valid": res["valid"], "errors": _errors_for(res), "attributes": attrs,
                            "register": {"key": lic["register"], "url": lic["register_url"], "status": lic["status"]}})
        pairs_valid = pairs_invalid = 0
        examples = []
        for a, b in itertools.combinations(sorted(lics, key=lambda r: r["name"]), 2):
            auths = closure(onto, jur, [a["name"], b["name"]])
            res = validate_with(onto, _with_flags(onto, {"jurisdictions": [jur], "authorisations": auths}))
            if res["valid"]:
                pairs_valid += 1
                if len(examples) < max_examples:
                    examples.append({"authorisations": auths})
            else:
                pairs_invalid += 1
        out["jurisdictions"].append({
            "jurisdiction": jur, "name": onto.jurisdictions[jur]["name"], "regulators": onto.jurisdictions[jur].get("regulators", []),
            "licences": len(lics), "products_reachable": len({p for s in singles for p in s["products"]}),
            "single_licence_permutations": singles,
            "licence_pairs": {"valid": pairs_valid, "invalid": pairs_invalid, "examples": examples},
        })
    return out


def similar_profiles(engine: Engine, attributes: dict, *, exclude_id: str | None = None, limit: int = 10) -> dict:
    """Stored profiles sharing products / authorisations, plus the jurisdictions
    where the same product set is reachable ('organisations like this exist
    in these jurisdictions')."""
    products = {str(p).lower() for p in attributes.get("products") or []}
    auths = {str(a).lower() for a in attributes.get("authorisations") or []}
    with engine.connect() as conn:
        onto = Ontology(conn)
        rows = [dict(r) for r in conn.execute(sa.select(profiles).where(profiles.c.valid_to.is_(None))
                                              .where(profiles.c.status == "valid")).mappings()]
    scored = []
    for r in rows:
        if r["id"] == exclude_id:
            continue
        rp = {str(p).lower() for p in r["attributes"].get("products") or []}
        ra = {str(a).lower() for a in r["attributes"].get("authorisations") or []}
        overlap = len(products & rp) + len(auths & ra)
        union = len(products | rp) + len(auths | ra)
        if overlap:
            scored.append({"id": r["id"], "name": r["name"], "jurisdictions": r["attributes"].get("jurisdictions", []),
                           "similarity": round(overlap / union, 3) if union else 0.0, "shared_products": sorted(products & rp),
                           "shared_authorisations": sorted(auths & ra)})
    scored.sort(key=lambda s: -s["similarity"])
    reachable = []
    want_ids = {onto.products.resolve(p)["id"] for p in products if onto.products.resolve(p)}
    for jur in sorted(onto.jurisdictions):
        lic_ids = {l["id"] for l in onto.licences.rows.values() if l["jurisdiction"].upper() == jur}
        covered = {p["product_id"] for p in onto.permits if p["licence_id"] in lic_ids}
        if want_ids and want_ids <= covered:
            via = sorted({onto.licences.rows[p["licence_id"]]["name"] for p in onto.permits
                          if p["licence_id"] in lic_ids and p["product_id"] in want_ids})
            reachable.append({"jurisdiction": jur, "name": onto.jurisdictions[jur]["name"], "via": via})
    return {"similar": scored[:limit], "same_products_reachable_in": reachable}

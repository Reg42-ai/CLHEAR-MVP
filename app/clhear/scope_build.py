"""Build a scoped corpus from L1 up, one layer at a time.

    python -m app.clhear.scope_build --scope compliance-program-demo [--skip-import]
        [--profile profile.json] [--publish-release]

Run against a database that holds only the scope (``CLHEAR_SOURCE_SCOPE``).
L1 imports each scoped document through the same adapters, verification and
acceptance as every other import. Each later layer runs the production
derivation for that layer — no curated blocks, activities, profiles, concepts,
register snapshot or alias tables — and records a build (``layer_builds``)
naming the input revisions it read. A layer whose input changed after that
input's last build does not run.
"""
from __future__ import annotations

import argparse
import json
import logging
import sys
from datetime import datetime, timezone

from sqlalchemy.engine import Engine

from app.clhear import layer_builds
from app.clhear.l1 import scopes

log = logging.getLogger("clhear.scope_build")


def import_sources(engine: Engine, llm, scope: dict) -> dict:
    from app.clhear.l1 import source_registry
    from app.clhear.workers import AdapterRunIncomplete, run_adapter_fleet

    source_registry.seed(engine)
    lanes = {}
    for adapter, keys in scope["imports"].items():
        try:
            result = run_adapter_fleet(engine, adapter, source_keys=list(keys), discover=False, trigger="scope_build",
                                       event_key=f"scope-build:{scopes.active_name()}:{adapter}:{datetime.now(timezone.utc).date()}")
            lanes[adapter] = {"statuses": result.get("statuses"), "acceptance": result.get("acceptance")}
        except AdapterRunIncomplete as exc:
            # Verified imports stay in force; acceptance findings are recorded, not hidden.
            lanes[adapter] = {"incomplete": str(exc)[:300]}
    return lanes


def derive_l2(engine: Engine, llm) -> dict:
    from app.clhear.l2.consolidate import draft_and_propose
    from app.clhear.l2.dedupe import consolidate
    from app.clhear.l2.extract import run_extraction
    from app.clhear.l2.review import review_obligations
    from app.clhear.l2.structured import refine_structured
    from app.clhear.l2.triage import triage_duties

    return {"extraction": run_extraction(engine), "triage": triage_duties(engine, llm),
            "structured": refine_structured(engine, llm), "consolidation": draft_and_propose(engine, llm),
            "dedupe": consolidate(engine), "review": review_obligations(engine, llm)}


def derive_l3(engine: Engine, llm) -> dict:
    from app.clhear.curated import seed_data_model
    from app.clhear.l3.characterize import characterize
    from app.clhear.l3.decompose import decompose
    from app.clhear.l3.generate import generate_blocks
    from app.clhear.l3.harmonize import harmonize

    return {"data_model": seed_data_model(engine), "generated": generate_blocks(engine, llm),
            "decomposition": decompose(engine), "harmonisation": harmonize(engine),
            "characterisation": characterize(engine, llm)}


def derive_l4(engine: Engine, llm, profiles: list[dict]) -> dict:
    from app.clhear.l4.licenses import extract_licenses
    from app.clhear.l4.ontology import build_ontology
    from app.clhear.l4.predicates import extract_predicates
    from app.clhear.l4.validate import create_profile, revalidate_profiles

    licenses = extract_licenses(engine, llm)
    ontology = build_ontology(engine, check_registers=False)
    predicates = extract_predicates(engine, llm)
    stored = []
    for profile in profiles:
        # Tenant-submitted self-descriptions, validated against the derived ontology.
        row = create_profile(engine, profile["attributes"], name=profile.get("name", ""), source="api", allow_invalid=True)
        stored.append({"id": row["id"], "status": row.get("status"), "name": profile.get("name", "")})
    return {"licenses": licenses, "ontology": {"version": ontology["version"], "merged": ontology["license_types_merged"]},
            "predicates": predicates, "profiles": stored, "revalidation": revalidate_profiles(engine)}


def derive_l5(engine: Engine, llm) -> dict:
    from app.clhear.l5.check import check_junction
    from app.clhear.l5.map import map_activities

    mapping = map_activities(engine, llm)
    junction = check_junction(engine)
    return {"mapping": mapping, "junction": {k: junction[k] for k in ("activities", "edges", "ok")},
            "orphans": len(junction["orphans"]), "dangling": len(junction["dangling"])}


def derive_l6(engine: Engine, llm) -> dict:
    from app.clhear.fleets import compose_stored_profiles
    from app.clhear.l6 import composer
    from app.clhear.l6.explain import refine_explanations

    composed = compose_stored_profiles(engine)
    explained = []
    with engine.connect() as conn:
        current = [composer.get_blueprint(conn, r["blueprint_id"]) for r in composer.list_blueprints(conn, status="current", limit=20)]
    for bp in current:
        if bp and bp["composition"].get("items"):
            explained.append(refine_explanations(engine, llm, dict(bp["composition"], blueprint_id=bp["blueprint_id"])))
    return {"composed": composed, "explained": len(explained)}


def derive_l7(engine: Engine, llm) -> dict:
    from app.clhear.l7 import enforcement, score

    return {"events": enforcement.ingest_events(engine), "links": enforcement.link_events(engine, llm),
            "calibration": {k: v for k, v in score.calibrate(engine).items() if k in ("status", "id", "held_out_year")},
            "obligation_scores": {k: v for k, v in score.score_obligations(engine).items() if k != "bands"}}


def item_priority(engine: Engine) -> dict:
    from app.clhear.l7 import score

    return score.score_items(engine)


def derive_l8(engine: Engine, llm) -> dict:
    from app.clhear.l8.reference import reference_rows

    rows = reference_rows(engine)
    return {"reference_rows": len(rows), "mapped_to_blocks": sum(1 for r in rows if r["block_id"])}


def _counts(engine: Engine, layer: str) -> dict:
    with engine.connect() as conn:
        if layer == "L8":
            from app.clhear.l8.reference import derived_reference_rows

            return {"reference_rows": len(derived_reference_rows(conn))}
        return {t.name: len(layer_builds._rows(conn, t)) for t in layer_builds._tables(layer)}


def build(engine: Engine, llm, *, skip_import: bool = False, profiles: list[dict] | None = None,
          layers: tuple[str, ...] = layer_builds.ORDER) -> dict:
    scope = scopes.active()
    if scope is None:
        raise RuntimeError(f"Set {scopes.SCOPE_ENV}; a scope build never runs against the full registry")
    name = scopes.active_name()
    steps = {
        "L1": (lambda: {"skipped": "import"}) if skip_import else (lambda: import_sources(engine, llm, scope)),
        "L2": lambda: derive_l2(engine, llm), "L3": lambda: derive_l3(engine, llm),
        "L4": lambda: derive_l4(engine, llm, profiles or []), "L5": lambda: derive_l5(engine, llm),
        "L6": lambda: derive_l6(engine, llm), "L7": lambda: derive_l7(engine, llm), "L8": lambda: derive_l8(engine, llm),
    }
    report = {"scope": name, "layers": {}}
    for layer in layer_builds.ORDER:
        if layer not in layers:
            continue
        with engine.connect() as conn:
            inputs = layer_builds.check_inputs(conn, layer, name)
        started = datetime.now(timezone.utc)
        detail = steps[layer]()
        built = layer_builds.record(engine, layer, scope=name, inputs=inputs, counts=_counts(engine, layer),
                                    steps=detail, started_at=started)
        report["layers"][layer] = {"revision": built["revision"], "inputs": inputs, "counts": built["counts"]}
        log.info("built %s %s from %s", layer, built["revision"][:12], {k: v[:12] for k, v in inputs.items()})
    if "L7" in report["layers"]:
        view = "L7 item priority"
        with engine.connect() as conn:
            inputs = layer_builds.check_inputs(conn, view, name)
        report["views"] = {view: {"inputs": inputs, "item_scores": item_priority(engine)}}
    return report


def _router(engine: Engine):
    from app.clhear.platform.router import Router, build_providers

    providers = build_providers()
    if not providers or set(providers) == {"fake"}:
        raise RuntimeError("A scope build derives live layers; configure Reg42 Infer, not the fake provider")
    return Router(engine, providers=providers)


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.clhear.scope_build")
    parser.add_argument("--scope", required=True)
    parser.add_argument("--skip-import", action="store_true")
    parser.add_argument("--layers", default=",".join(layer_builds.ORDER))
    parser.add_argument("--profile", action="append", default=[], help="JSON file of a tenant-submitted L4 profile")
    parser.add_argument("--publish-release", action="store_true")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    import os

    if os.environ.get(scopes.SCOPE_ENV, "") != args.scope:
        parser.error(f"{scopes.SCOPE_ENV} must equal --scope before the registry loads")
    logging.basicConfig(level=logging.INFO)
    from app.clhear.db import get_engine, run_migrations

    engine = get_engine()
    run_migrations(engine)
    profiles = [json.loads(open(path).read()) for path in args.profile]
    report = build(engine, _router(engine), skip_import=args.skip_import, profiles=profiles,
                   layers=tuple(x.strip() for x in args.layers.split(",") if x.strip()))
    if args.publish_release:
        from app.clhear.scope_release import publish

        report["release"] = publish(engine, args.scope)
    print(json.dumps(report, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

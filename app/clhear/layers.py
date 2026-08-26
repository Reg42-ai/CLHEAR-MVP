"""Eight-layer contract (HLD §2). Only L0+L1 are published today.

App clients MUST feature-detect via release.layers and /v1/releases/{id}/l{n}
status. Reserved layers return 501 + layer_status=not_published, never 404.
"""

from __future__ import annotations

# Orientation-only until their HLDs ship. Do not invent product bodies here.
LAYER_CATALOG: dict[str, dict] = {
    "L0": {"slug": "l0", "name": "Platform rails", "schema": "l0_platform", "published": True},
    "L1": {"slug": "l1", "name": "Verbatim sources", "schema": "l1_sources", "published": True},
    "L2": {"slug": "l2", "name": "Obligation registry", "schema": "l2_obligations", "published": False},
    "L3": {"slug": "l3", "name": "Building blocks", "schema": "l3_building_blocks", "published": False},
    "L4": {"slug": "l4", "name": "Profile space", "schema": "l4_profiles", "published": False},
    "L5": {"slug": "l5", "name": "Activities", "schema": "l5_activities", "published": False},
    "L6": {"slug": "l6", "name": "Program composer", "schema": "l6_composer", "published": False},
    "L7": {"slug": "l7", "name": "Risk scoring", "schema": "l7_risk", "published": False},
    "L8": {"slug": "l8", "name": "Benchmarks", "schema": "l8_benchmarks", "published": False},
}

PUBLISHED_LAYERS = tuple(k for k, v in LAYER_CATALOG.items() if v["published"])
RESERVED_LAYERS = tuple(k for k, v in LAYER_CATALOG.items() if not v["published"])
LAYER_SLUGS = {v["slug"]: k for k, v in LAYER_CATALOG.items()}


def normalize_layer(raw: str) -> str | None:
    token = (raw or "").strip()
    if not token:
        return None
    upper = token.upper()
    if upper in LAYER_CATALOG:
        return upper
    return LAYER_SLUGS.get(token.lower())


def not_published_body(layer: str) -> dict:
    meta = LAYER_CATALOG.get(layer, {})
    return {
        "layer": layer,
        "layer_status": "not_published",
        "name": meta.get("name"),
        "schema": meta.get("schema"),
        "detail": f"{layer} is reserved. CLHEAR currently publishes {', '.join(PUBLISHED_LAYERS)} only.",
    }

"""Eight-layer contract (HLD §2) — the system's spine.

Every layer carries a machine-readable *derivation contract*: how the layer
determines the data it has (inputs, method, human/eval gates, evidence).
Statuses:
  live   — real data, produced by the described method (L0, L1 today)
  demo   — browsable illustrative data, authored to preview the layer's shape;
           every demo item still chains down to REAL L1 clauses (lineage)
  locked — closed by design (L8): definition visible, data not

App clients MUST feature-detect via release.layers and /v1/releases/{id}/l{n}
status. Only `live` layers are `published` for the /v1 contract; demo layers
answer with layer_status="demo" and demo-labeled bodies, never as real data.
"""

from __future__ import annotations

LAYER_CATALOG: dict[str, dict] = {
    "L0": {
        "slug": "l0",
        "name": "Platform rails",
        "schema": "l0_platform",
        "published": True,
        "status": "live",
        "purpose": "The rails every layer runs on: one event bus, one LLM gateway "
        "with cost control, one human-approval queue, one release pipeline.",
        "derivation": {
            "inputs": [],
            "method": "Deterministic infrastructure. Events are written to a transactional "
            "outbox in the same transaction as the data change, then relayed to SQS; "
            "every fleet run is recorded in an append-only ledger; every LLM call is "
            "logged with prompt hash, tokens and cost under hard daily spend caps.",
            "gates": [
                "Named-human approvals: no proposal takes effect without a recorded maintainer identity + timestamp",
                "Hard LLM spend caps ($20/day per fleet, $100/day global) — hard stop, not a warning",
            ],
            "evidence": [
                "runs ledger (replayable: same inputs => same corpus)",
                "llm_calls ledger with per-call cost",
                "outbox relayed_at stamps",
            ],
        },
    },
    "L1": {
        "slug": "l1",
        "name": "Verbatim sources",
        "schema": "l1_sources",
        "published": True,
        "status": "live",
        "purpose": "The vault of regulatory truth: official texts fetched from "
        "authoritative sources, split into clauses, stored immutably, watched forever.",
        "derivation": {
            "inputs": ["L0"],
            "method": "Deterministic fetch -> parse -> hash -> diff. Adapters retrieve the "
            "official artifact from the issuing authority (legislation.gov.uk, EUR-Lex, "
            "govinfo/eCFR, NIST); the pipeline stores originals in WORM S3, versions the "
            "text, aligns clauses by ref and emits clause-level change events. Source "
            "text is NEVER generated, cleaned up, or summarized into the record.",
            "gates": [
                "Fidelity checks against the fetched original before a version is accepted",
                "Family membership changes go through the L0 proposals queue (human ratifies)",
                "Restricted-license texts (ISO, TSC, PCI, IFRS) expose refs and hashes only",
            ],
            "evidence": [
                "content_hash per version, text_hash per clause",
                "immutable S3 original per version (Object Lock)",
                "E1-E7 eval scorecards per source",
                "clause-level change_events with diffs",
            ],
        },
    },
    "L2": {
        "slug": "l2",
        "name": "Obligation registry",
        "schema": "l2_obligations",
        "published": False,
        "status": "demo",
        "purpose": "Atomic obligations — who must do what, when — extracted from L1 "
        "clauses and kept current by clause-level change inference.",
        "derivation": {
            "inputs": ["L1"],
            "method": "Each obligation is anchored to the exact L1 clause(s) that impose it. "
            "Extraction is deterministic-first (clause structure, duty verbs, addressee "
            "detection); LLM assistance is confined to the gated triage step; every "
            "obligation stores its basis refs so it can be re-verified against the "
            "verbatim text at any time. When L1 detects a clause change, affected "
            "obligations are flagged for re-derivation automatically.",
            "gates": [
                "Maintainer approval per obligation via the L0 proposals queue",
                "Re-derivation flag on any L1 change event touching a basis clause",
                "Restricted sources contribute obligation refs only, never text",
            ],
            "evidence": [
                "basis clause refs + hashes per obligation (inspect the lineage)",
                "change-propagation trail from L1 change_events",
            ],
        },
    },
    "L3": {
        "slug": "l3",
        "name": "Building blocks",
        "schema": "l3_building_blocks",
        "published": False,
        "status": "demo",
        "purpose": "Reusable compliance capabilities (a CDD programme, a breach-response "
        "process, an access-control regime) that satisfy sets of L2 obligations.",
        "derivation": {
            "inputs": ["L2"],
            "method": "Blocks are composed by clustering L2 obligations that share a "
            "control surface: same operational capability, same evidence artifacts. "
            "Each block declares exactly which obligations it satisfies and what "
            "implementing it requires; the mapping is many-to-many and every edge "
            "is inspectable down to the underlying clauses.",
            "gates": [
                "Block-to-obligation mappings ratified by a maintainer",
                "A block loses its 'satisfies' edge automatically if a mapped obligation is re-derived",
            ],
            "evidence": ["obligation mapping per block, each traceable to L1 clauses"],
        },
    },
    "L4": {
        "slug": "l4",
        "name": "Profile space",
        "schema": "l4_profiles",
        "published": False,
        "status": "demo",
        "purpose": "The dimensions that determine which rules apply to an organisation: "
        "jurisdictions, licenses, products, customer base, data footprint.",
        "derivation": {
            "inputs": ["L2", "L5"],
            "method": "Profiles are declared, not inferred: an organisation states its "
            "facts (where it operates, what licenses it holds, what it sells, to whom). "
            "The profile schema itself is derived from the applicability conditions "
            "found in L2 obligations — every profile attribute exists because some "
            "obligation's scope depends on it.",
            "gates": [
                "Profile attributes added only when an obligation's applicability requires them",
            ],
            "evidence": ["per-attribute list of the obligations whose scope reads it"],
        },
    },
    "L5": {
        "slug": "l5",
        "name": "Activities",
        "schema": "l5_activities",
        "published": False,
        "status": "demo",
        "purpose": "The business-compliance junction: concrete things a business does "
        "(onboard a customer, custody crypto-assets, run marketing) joined to the "
        "obligations those activities trigger.",
        "derivation": {
            "inputs": ["L2", "L4"],
            "method": "Each activity is mapped to the L2 obligations it triggers, "
            "conditioned on L4 profile facts (the same activity triggers different "
            "obligations for a UK EMI vs an EU CASP). Mappings cite the applicability "
            "language in the underlying clauses.",
            "gates": ["Activity-to-obligation mappings ratified by a maintainer"],
            "evidence": ["trigger mapping per activity with profile conditions + clause basis"],
        },
    },
    "L6": {
        "slug": "l6",
        "name": "Program composer",
        "schema": "l6_composer",
        "published": False,
        "status": "demo",
        "purpose": "Composes a concrete compliance program for one profile: the set of "
        "L3 building blocks that covers every obligation the profile's activities trigger.",
        "derivation": {
            "inputs": ["L3", "L4", "L5"],
            "method": "Deterministic set-cover: take the profile (L4), enumerate its "
            "activities (L5), collect every triggered obligation (L2), then select "
            "building blocks (L3) until the obligation set is covered. The output is "
            "a coverage matrix — obligation x block — where every cell is explainable "
            "and every gap is explicit.",
            "gates": [
                "Coverage gaps are surfaced, never silently accepted",
                "Program versions pinned to a CLHEAR release (obligations don't drift under a program)",
            ],
            "evidence": ["coverage matrix per program; gap list; release pin"],
        },
    },
    "L7": {
        "slug": "l7",
        "name": "Risk scoring",
        "schema": "l7_risk",
        "published": False,
        "status": "demo",
        "purpose": "Quantifies exposure per program area: where coverage is thin, where "
        "the underlying law is changing fastest, where evidence is weakest.",
        "derivation": {
            "inputs": ["L1", "L6"],
            "method": "Scores are computed, not judged: inputs are the L6 coverage ratio, "
            "the count of open obligations, and the live L1 change velocity of the "
            "underlying sources (regulatory churn measured from clause-level change "
            "events). Every score publishes its formula and its inputs.",
            "gates": ["Formula and weights are versioned; a score without its inputs is invalid"],
            "evidence": ["per-score input vector including live L1 change counts"],
        },
    },
    "L8": {
        "slug": "l8",
        "name": "Benchmarks",
        "schema": "l8_benchmarks",
        "published": False,
        "status": "locked",
        "purpose": "Closed peer benchmarks: how a program's coverage and risk posture "
        "compare across anonymized peers in the same profile cluster.",
        "derivation": {
            "inputs": ["L7"],
            "method": "Aggregates L7 scores across participating organisations within a "
            "profile cluster. Closed by design: raw peer data never leaves the enclave; "
            "only k-anonymous aggregates are computed, and none are published today.",
            "gates": [
                "k-anonymity threshold before any aggregate exists",
                "Participation is opt-in and contractual",
            ],
            "evidence": ["aggregate definitions (visible); data (locked)"],
        },
    },
}

PUBLISHED_LAYERS = tuple(k for k, v in LAYER_CATALOG.items() if v["published"])
RESERVED_LAYERS = tuple(k for k, v in LAYER_CATALOG.items() if not v["published"])
DEMO_LAYERS = tuple(k for k, v in LAYER_CATALOG.items() if v["status"] == "demo")
LAYER_ORDER = tuple(sorted(LAYER_CATALOG, key=lambda k: int(k[1:])))
LAYER_SLUGS = {v["slug"]: k for k, v in LAYER_CATALOG.items()}


def normalize_layer(raw: str) -> str | None:
    token = (raw or "").strip()
    if not token:
        return None
    upper = token.upper()
    if upper in LAYER_CATALOG:
        return upper
    return LAYER_SLUGS.get(token.lower())


def layer_public_meta(layer: str) -> dict:
    """Catalog entry shaped for API/UI consumption."""
    meta = LAYER_CATALOG[layer]
    return {
        "layer": layer,
        "slug": meta["slug"],
        "name": meta["name"],
        "schema": meta["schema"],
        "status": meta["status"],
        "published": meta["published"],
        "purpose": meta["purpose"],
        "derivation": meta["derivation"],
    }


def not_published_body(layer: str) -> dict:
    meta = LAYER_CATALOG.get(layer, {})
    return {
        "layer": layer,
        "layer_status": "not_published",
        "name": meta.get("name"),
        "schema": meta.get("schema"),
        "detail": f"{layer} is reserved. CLHEAR currently publishes {', '.join(PUBLISHED_LAYERS)} only.",
    }


def demo_banner(layer: str) -> dict:
    """The honesty label attached to every demo-layer payload."""
    meta = LAYER_CATALOG.get(layer, {})
    return {
        "data_status": "demo",
        "notice": f"{layer} ({meta.get('name')}) is shown with illustrative data authored "
        "to preview the layer's shape. It is NOT derived output. Lineage links point at "
        "real L1 clauses so the derivation chain itself is genuine.",
    }

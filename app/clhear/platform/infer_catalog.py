"""Catalog for `clhear-infer`, CLHEAR's private Reg42 Infer router (HLD v2 I6).

CLHEAR is a product, not a workforce seat: it runs its own Infer instance (same image,
pinned by digest) so its derivation spend never draws on the employees' compute budget
and its routing can only reach procurement-clean Bedrock models. Infer reads five YAML
files from ``/catalog``; this module renders all of them from
``task_classes.py`` so the ladders CLHEAR publishes and the routes Infer applies are the
same bytes. ``scripts/render_infer_catalog.py`` writes them to ``infra/infer-catalog/``
and a test keeps the committed files identical to the render.

Catalog entries are keyed by Bedrock id: Infer reports the key as the response ``model``,
so CLHEAR's ledger, price table and release manifest see Bedrock ids, not nicknames.
"""
from __future__ import annotations

from app.clhear.platform import task_classes as tc
from app.clhear.platform.gateway import BEDROCK_PRICING

# Infer image the private router runs; bumping it is a deliberate change reviewed here.
INFER_IMAGE = (
    "730649732189.dkr.ecr.us-east-1.amazonaws.com/workforce-dev-infer"
    "@sha256:331661f9ef49a3c793ade5e16463100d87333c46f6900db9a37dde671d5c311d"
)
PRINCIPAL = "clhear"

# Models Bedrock answered for this account (Converse probe, 2026-09-13). Anything not
# listed here is disabled in the catalog even if a ladder names it.
AVAILABLE: frozenset[str] = frozenset({
    tc.GPT_OSS_120B, tc.MISTRAL_LARGE_3, tc.CLAUDE_OPUS, tc.NOVA_LITE, tc.NOVA_PRO,
    tc.LLAMA_3_3_70B, tc.TITAN_EMBED_V2,
})

_MODEL_META: dict[str, dict] = {
    tc.GPT_OSS_120B: {"license": "open_weight", "context": 128000},
    tc.MISTRAL_LARGE_3: {"license": "commercial", "context": 128000},
    tc.CLAUDE_OPUS: {"license": "commercial", "context": 200000},
    tc.CLAUDE_SONNET: {"license": "commercial", "context": 200000,
                       "note": "needs the account's Anthropic use-case form accepted"},
    tc.NOVA_PRO: {"license": "commercial", "context": 300000},
    tc.NOVA_LITE: {"license": "commercial", "context": 300000},
    tc.LLAMA_3_3_70B: {"license": "open_weight", "context": 128000},
    tc.TITAN_EMBED_V2: {"license": "commercial", "context": 8000, "role": "embed"},
    tc.COHERE_EMBED_MULTI: {"license": "commercial", "context": 512, "role": "embed",
                            "note": "not probed in this account"},
}

_TIER_NAMES: dict[tuple[str, ...], str] = {
    tc._DERIVATION_LADDER: "clhear-derivation",
    tc._HARD_LADDER: "clhear-hard",
    tc._CHEAP_LADDER: "clhear-cheap",
    tc._NONDERIVATION_LADDER: "clhear-nonderivation",
}


def tier_for(task_class: tc.TaskClass) -> str:
    if task_class.id == "embed":
        return "embed"
    return _TIER_NAMES.get(task_class.ladder, f"clhear-{task_class.id}")


def tiers() -> dict[str, tuple[str, ...]]:
    out: dict[str, tuple[str, ...]] = {}
    for t in tc.TASK_CLASS_LIST:
        out.setdefault(tier_for(t), t.ladder)
    return out


def catalog_models() -> list[str]:
    seen: list[str] = []
    for ladder in tiers().values():
        for m in ladder:
            if m not in seen:
                seen.append(m)
    if tc.CLAUDE_SONNET not in seen:
        seen.append(tc.CLAUDE_SONNET)
    return seen


def _q(s: str) -> str:
    return f'"{s}"'


def render_models() -> str:
    lines = [
        "# clhear-infer model catalog — generated from app/clhear/platform/task_classes.py",
        "# (CLHEAR-MVP, scripts/render_infer_catalog.py). Do not hand-edit.",
        "# Entries are keyed by Bedrock id so Infer reports Bedrock ids back to CLHEAR.",
        "# Only procurement-clean (US/EU origin) models exist in this catalog; Infer's",
        "# last-resort picker therefore cannot reach anything else.",
        "",
        "defaults:",
        "  backend: bedrock",
        "  fallback_backend: bedrock",
        "  escalate_after: 2",
        "",
        "tiers:",
    ]
    for name, ladder in tiers().items():
        champion, *fallbacks = ladder
        lines.append(f"  {name}:")
        lines.append(f"    champion: {_q(champion)}")
        lines.append("    fallbacks: [" + ", ".join(_q(m) for m in fallbacks) + "]")
    lines += ["", "models:"]
    for m in catalog_models():
        meta = _MODEL_META[m]
        price_in, price_out = BEDROCK_PRICING[m]
        lines.append(f"  {_q(m)}:")
        lines.append(f"    id: {_q(m)}")
        lines.append("    backend: bedrock")
        lines.append(f"    license: {meta['license']}")
        lines.append(f"    origin: {tc.origin_of(m)}")
        lines.append(f"    usd_per_1m_in: {price_in:.4f}")
        lines.append(f"    usd_per_1m_out: {price_out:.4f}")
        lines.append(f"    tool_call: {'false' if meta.get('role') == 'embed' else 'true'}")
        lines.append(f"    context: {meta['context']}")
        if meta.get("role"):
            lines.append(f"    role: {meta['role']}")
        lines.append(f"    enabled: {'true' if m in AVAILABLE else 'false'}")
        if meta.get("note"):
            lines.append(f"    note: {meta['note']}")
    return "\n".join(lines) + "\n"


def render_policy() -> str:
    lines = [
        "# clhear-infer routing — generated from app/clhear/platform/task_classes.py. Do not hand-edit.",
        "# One route per CLHEAR task class (X-Task-Class); anything else lands on the",
        "# derivation tier, never on a non-clean model (there are none in models.yaml).",
        "",
        "defaults:",
        "  fallback: bedrock",
        "  escalate_after: 2",
        "  max_tokens: 4096",
        "  timeout_seconds: 120",
        "  tool_required_tier: clhear-derivation",
        "",
        "routes:",
    ]
    for t in tc.TASK_CLASS_LIST:
        lines.append(f"  - id: {t.id}")
        lines.append("    match:")
        lines.append(f"      task_class: [{t.id}]")
        lines.append(f"    tier: {tier_for(t)}")
    lines += [
        "  - id: clhear-default",
        "    match: {}",
        "    tier: clhear-derivation",
        "",
        "employee_defaults:",
        f"  {PRINCIPAL}: {{ task_class: l2_extract }}",
    ]
    return "\n".join(lines) + "\n"


def render_employees(daily_usd_cap: float) -> str:
    return "\n".join([
        "# clhear-infer principals. Infer's schema calls these employees; CLHEAR has exactly",
        "# one — the product itself. Generated by scripts/render_infer_catalog.py.",
        "company:",
        "  name: CLHEAR",
        "  goal: Derive the open regulatory record (L1-L8) on procurement-clean Bedrock models.",
        "",
        "employees:",
        f"  - id: {PRINCIPAL}",
        "    title: CLHEAR derivation fleets and web tier (product principal, not a seat)",
        "    status: hired",
        "    tier: core",
        "    adapter: clhear-gateway",
        f"    daily_usd_cap: {daily_usd_cap:g}",
        "    mission: Nightly derivation, second-model review, embeddings, release manifest.",
    ]) + "\n"


def render_budget(monthly_usd_cap: float) -> str:
    return "\n".join([
        "# clhear-infer hard stop. Infer refuses every call once the month's spend reaches",
        "# company.hired_usd — this is CLHEAR's own ceiling, separate from the workforce budget.",
        "company:",
        "  name: CLHEAR",
        f"  monthly_usd: {monthly_usd_cap:g}",
        f"  hired_usd: {monthly_usd_cap:g}",
        "  headroom_usd: 0",
    ]) + "\n"


def render_tools() -> str:
    return "\n".join([
        "# clhear-infer: no tools. CLHEAR calls chat completions and embeddings only.",
        "defaults: {}",
        "seats: {}",
        "never: []",
    ]) + "\n"


def render_dockerfile() -> str:
    return "\n".join([
        "# clhear-infer: the Reg42 Infer image pinned by digest, with CLHEAR's catalog baked in.",
        "# Generated by scripts/render_infer_catalog.py; bump INFER_IMAGE there to upgrade.",
        f"FROM {INFER_IMAGE}",
        "COPY models.yaml policy.yaml employees.yaml budget.yaml tools.yaml /catalog/",
    ]) + "\n"


def render_all(*, daily_usd_cap: float, monthly_usd_cap: float) -> dict[str, str]:
    return {
        "models.yaml": render_models(),
        "policy.yaml": render_policy(),
        "employees.yaml": render_employees(daily_usd_cap),
        "budget.yaml": render_budget(monthly_usd_cap),
        "tools.yaml": render_tools(),
        "Dockerfile": render_dockerfile(),
    }

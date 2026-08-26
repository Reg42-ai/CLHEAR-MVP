"""Generate plain-language clause explainers for the corpus (Tier 2).

Batches every un-annotated public clause of the in-force versions through the
L0 gateway (fleet `l1.annotate`, structured output, spend-capped, all calls in
the llm_calls ledger). Idempotent: re-runs only touch new/changed clauses.

Requires ANTHROPIC_API_KEY (env or settings). ~2,100 clauses ≈ $2 at
Haiku-class pricing, well inside the $20/day fleet cap.

Usage:
    DATABASE_URL=sqlite:///deploy/clhear.db python scripts/annotate_corpus.py [max_clauses]
"""
import json
import logging
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from app.clhear.db import get_engine, run_migrations  # noqa: E402
from app.clhear.l1 import annotate  # noqa: E402
from app.clhear.platform.gateway import AnthropicProvider, Gateway  # noqa: E402
from app.clhear.settings import get_settings  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    settings = get_settings()
    if not settings.anthropic_api_key:
        print(
            "ANTHROPIC_API_KEY is not configured — the explainer job needs it.\n"
            "Add it as a Cloud Agent secret or set SSM /clhear/ANTHROPIC_API_KEY, then re-run.",
            file=sys.stderr,
        )
        return 2
    max_clauses = int(sys.argv[1]) if len(sys.argv) > 1 else None
    engine = get_engine()
    run_migrations(engine)
    gateway = Gateway(engine, AnthropicProvider())
    summary = annotate.annotate_llm(engine, gateway, max_clauses=max_clauses)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

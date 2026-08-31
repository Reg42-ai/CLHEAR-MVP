"""Generate plain-language clause explainers for the corpus (Tier 2).

Batches every un-annotated public clause of the in-force versions through the
router (fleet `l1.annotate`, structured output, spend-capped, all calls in
the llm_calls ledger). Idempotent: re-runs only touch new/changed clauses.

Uses local Ollama (qwen3.5:9b) when OLLAMA_BASE_URL is set. No vendor key.

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
from app.clhear.platform.router import live_llm  # noqa: E402


def main() -> int:
    logging.basicConfig(level=logging.INFO)
    engine = get_engine()
    run_migrations(engine)
    gateway = live_llm(engine)
    if gateway is None:
        print(
            "No Ollama endpoint configured — set OLLAMA_BASE_URL "
            "(local sidecar, no key) or OLLAMA_API_KEY for ollama.com.",
            file=sys.stderr,
        )
        return 2
    max_clauses = int(sys.argv[1]) if len(sys.argv) > 1 else None
    summary = annotate.annotate_llm(engine, gateway, max_clauses=max_clauses)
    print(json.dumps(summary, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

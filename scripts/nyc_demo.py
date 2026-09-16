"""Compatibility entry point; fixtures live only in the isolated test harness."""
from pathlib import Path
import sys
sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from tests.support.journey import Journey as Demo, seed_synthetic_corpus, start_app, main

if __name__ == "__main__":
    raise SystemExit(main())

"""Compatibility entry point; operational verification belongs to the L0 worker.

Use ``python -m app.clhear.platform.release_verification`` for read-only local
inspection. Promotion calls that same verifier from CLHEAR's release worker.
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clhear.platform.release_verification import main, verify  # noqa: E402,F401


if __name__ == "__main__":
    sys.exit(main())

"""DR drill entry point (HLD v2 §7.1; item 17) — thin wrapper over
``app.clhear.platform.dr`` so the documented command works from a checkout:

    python scripts/dr_drill.py run [--scratch-url URL] [--release R] [--report PATH] [--skip-datalake]
    python scripts/dr_drill.py last
    python scripts/dr_drill.py history

Exit code 1 when the drill fails (the scheduled job turns red).
"""
from __future__ import annotations

import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clhear.platform.dr import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

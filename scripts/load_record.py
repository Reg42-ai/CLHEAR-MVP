#!/usr/bin/env python3
"""Load the SQLite snapshot into Aurora. Thin wrapper; see app/clhear/tools/load_record.py.

    python scripts/load_record.py --source ./clhear-latest.db --target postgresql+psycopg://...
"""
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clhear.tools.load_record import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())

#!/usr/bin/env python3
"""Render the clhear-infer catalog (infra/infer-catalog/) from task_classes.py.

    python scripts/render_infer_catalog.py [--daily 250] [--monthly 2000] [--check]

--check exits 1 when the committed files differ from the render (CI).
"""
from __future__ import annotations

import argparse
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clhear.platform import infer_catalog  # noqa: E402

OUT = Path(__file__).resolve().parents[1] / "infra" / "infer-catalog"
DEFAULT_DAILY = 250.0
DEFAULT_MONTHLY = 2000.0


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--daily", type=float, default=DEFAULT_DAILY, help="USD per day for the clhear principal")
    ap.add_argument("--monthly", type=float, default=DEFAULT_MONTHLY, help="USD per month hard stop")
    ap.add_argument("--check", action="store_true")
    args = ap.parse_args()
    files = infer_catalog.render_all(daily_usd_cap=args.daily, monthly_usd_cap=args.monthly)
    if args.check:
        stale = [n for n, body in files.items() if not (OUT / n).exists() or (OUT / n).read_text(encoding="utf-8") != body]
        if stale:
            print("stale:", ", ".join(stale))
            return 1
        print("infra/infer-catalog is current")
        return 0
    OUT.mkdir(parents=True, exist_ok=True)
    for name, body in files.items():
        (OUT / name).write_text(body, encoding="utf-8")
        print("wrote", OUT / name)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

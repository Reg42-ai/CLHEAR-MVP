"""Pin a CLHEAR L1 release and print the five dry-run checklist.

Week of 21 Sep 2026: publish once, pin that id, do not EOD-overwrite it
during meeting week.

Usage:
  python -m scripts.sept28_rehearse --publish --pin
  python -m scripts.sept28_rehearse --status
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

# Dry-run script (five times before 28 Sep):
DRY_RUNS = [
    "1. clhear.reg42.ai explorer: open FINRA Rule 2210 on the pinned release.",
    "2. OS banner shows the same release id and layers L0+L1 (L2–L8 not in this release).",
    "3. Galaxy Securities LLC on galaxy.app.reg42.ai — Data workspace: HR, campaigns, clients via ingest APIs.",
    "4. Open synced 2210 clause; overlay shows principal, WSP, approval, monitoring TEC.",
    "5. Map + gap → unapproved campaign + missing HR title; POST campaign; score moves; install Safeluance; case cites the L1 clause id.",
]


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--publish", action="store_true")
    parser.add_argument("--pin", action="store_true")
    parser.add_argument("--release-id", default="")
    parser.add_argument("--status", action="store_true")
    args = parser.parse_args()

    from app.clhear.db import get_engine, run_migrations
    from app.clhear.releases import get_latest, get_release, publish_release, release_id_for
    from app.clhear.settings import get_settings

    engine = get_engine()
    run_migrations(engine)
    settings = get_settings()
    pin_path = Path(settings.clhear_artifacts_dir) / "releases" / "pinned.json"

    if args.publish:
        rid = args.release_id or release_id_for()
        man = publish_release(engine, release_id=rid)
        print(json.dumps(man, indent=2))
        if args.pin:
            pin_path.parent.mkdir(parents=True, exist_ok=True)
            pin_path.write_text(json.dumps({"id": rid, "pinned": True}, indent=2))
            print(f"pinned {rid} -> {pin_path}")
        return 0

    if args.status:
        latest = get_latest(engine)
        pinned = json.loads(pin_path.read_text()) if pin_path.exists() else None
        print(json.dumps({"latest": latest, "pinned": pinned}, indent=2, default=str))
        return 0

    print("Sept 28 dry-run checklist (run five times):")
    for line in DRY_RUNS:
        print(f"  {line}")
    latest = get_latest(engine)
    if latest:
        print(f"\nCurrent latest release: {latest.get('id')}")
        print(f"Confirm get_release matches: {bool(get_release(latest['id'], engine=engine))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

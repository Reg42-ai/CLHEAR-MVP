"""Curated catalog: L3 building blocks, L5 activities, L4 attribute schema +
sample profiles, L8 benchmark definitions.

CURATED means human-authored policy content — not derived, not demo. It is
seeded from these reviewed JSON files into the derived-layer tables and from
then on changes only through the L0 proposals queue. Obligation references
are ANCHORS ({source_key, refs[]}) that resolve to machine-derived L2 rows at
read time, so curated mappings always point at the live registry.
"""
import json
from functools import lru_cache
from pathlib import Path

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.derived_models import activities, attribute_schema, blocks, sample_profiles

CURATED_DIR = Path(__file__).parent


@lru_cache
def load(name: str) -> list[dict]:
    return json.loads((CURATED_DIR / f"{name}.json").read_text())


def seed(engine: Engine) -> dict:
    """Idempotent upsert of the curated catalog into the DB."""
    counts = {"blocks": 0, "activities": 0, "attributes": 0, "profiles": 0}
    with engine.begin() as conn:
        for item in load("l3_building_blocks"):
            exists = conn.execute(sa.select(blocks.c.id).where(blocks.c.id == item["id"])).first()
            values = dict(
                name=item["name"], description=item.get("description", ""),
                capability=item.get("capability", ""),
                evidence_artifacts=item.get("evidence_artifacts", []),
                satisfies=item.get("satisfies", []),
                implements_controls=item.get("implements_controls", []),
                status="curated",
            )
            if exists:
                conn.execute(blocks.update().where(blocks.c.id == item["id"]).values(**values))
            else:
                conn.execute(blocks.insert().values(id=item["id"], **values))
            counts["blocks"] += 1
        for item in load("l5_activities"):
            exists = conn.execute(sa.select(activities.c.id).where(activities.c.id == item["id"])).first()
            values = dict(
                name=item["name"], description=item.get("description", ""),
                business_owner=item.get("business_owner", ""),
                triggers=item.get("triggers", []), status="curated",
            )
            if exists:
                conn.execute(activities.update().where(activities.c.id == item["id"]).values(**values))
            else:
                conn.execute(activities.insert().values(id=item["id"], **values))
            counts["activities"] += 1
        for item in load("l4_attribute_schema"):
            exists = conn.execute(
                sa.select(attribute_schema.c.key).where(attribute_schema.c.key == item["key"])
            ).first()
            values = dict(type=item["type"], description=item.get("description", ""), read_by=item.get("read_by", []))
            if exists:
                conn.execute(attribute_schema.update().where(attribute_schema.c.key == item["key"]).values(**values))
            else:
                conn.execute(attribute_schema.insert().values(key=item["key"], **values))
            counts["attributes"] += 1
        for item in load("l4_sample_profiles"):
            exists = conn.execute(
                sa.select(sample_profiles.c.id).where(sample_profiles.c.id == item["id"])
            ).first()
            values = dict(
                name=item["name"], description=item.get("description", ""),
                attributes=item.get("attributes", {}), activities=item.get("activities", []),
                status="sample",
            )
            if exists:
                conn.execute(sample_profiles.update().where(sample_profiles.c.id == item["id"]).values(**values))
            else:
                conn.execute(sample_profiles.insert().values(id=item["id"], **values))
            counts["profiles"] += 1
    return counts

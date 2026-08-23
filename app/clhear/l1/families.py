"""Family machinery (HLD §7.2): citator sync in P1.

Citator sync reads the OFFICIAL effects/relations feed of a family's root
source and auto-contains the affecting instruments as binding-tier family
members (added_via='citator'). Deterministic; citation mining + reconciliation
land in P2.
"""
import logging
import time

import sqlalchemy as sa
from sqlalchemy.engine import Engine

from app.clhear.l1.adapters.base import CitatorAdapter
from app.clhear.l1.models import family_members, source_families, sources
from app.clhear.models import runs
from app.clhear.platform import events as l0_events

log = logging.getLogger("clhear.l1.families")


def sync_citator(
    engine: Engine, adapter: CitatorAdapter, *, trigger: str = "manual", job_id: str | None = None
) -> dict:
    """Upsert one family-member row per official effect record."""
    started = time.monotonic()
    meta = adapter.meta()
    effects = adapter.family_effects()

    added: list[str] = []
    with engine.begin() as conn:
        family_id = conn.execute(
            sa.select(source_families.c.id).where(source_families.c.key == meta.family_key)
        ).scalar()
        if family_id is None:
            raise RuntimeError(f"family {meta.family_key} not ingested yet (run the pipeline first)")
        for effect in effects:
            source_id = conn.execute(
                sa.select(sources.c.id).where(sources.c.key == effect.affecting_key)
            ).scalar()
            if source_id is None:
                source_id = conn.execute(
                    sources.insert()
                    .values(
                        family_id=family_id,
                        key=effect.affecting_key,
                        name=effect.affecting_name,
                        kind=effect.kind,
                        issuer=meta.issuer,
                        jurisdiction=meta.jurisdiction,
                        license="open",
                        adapter=meta.adapter,
                        canonical_url=effect.affecting_url,
                    )
                    .returning(sources.c.id)
                ).scalar_one()
            member = conn.execute(
                sa.select(family_members.c.source_id)
                .where(family_members.c.family_id == family_id)
                .where(family_members.c.source_id == source_id)
            ).first()
            if member is None:
                conn.execute(
                    family_members.insert().values(
                        family_id=family_id,
                        source_id=source_id,
                        relation=effect.relation,
                        tier="binding",
                        status="active",
                        added_via="citator",
                    )
                )
                added.append(effect.affecting_key)
        if added:
            l0_events.emit(
                conn,
                layer="l1",
                kind="FamilyMembersAdded",
                subject_ref=meta.family_key,
                payload={"family": meta.family_key, "members": added, "via": "citator"},
                producer=f"l1.families.{meta.adapter}",
            )

    summary = {"family": meta.family_key, "effects": len(effects), "new_members": added, "status": "succeeded"}
    inputs = {"family": meta.family_key, "source": meta.source_key}
    if job_id:
        inputs["job_id"] = job_id
    with engine.begin() as conn:
        conn.execute(
            runs.insert().values(
                fleet="l1.citator",
                trigger=trigger,
                inputs=inputs,
                outputs=summary,
                duration_ms=int((time.monotonic() - started) * 1000),
            )
        )
    log.info("citator sync %s: %d effects, %d new members", meta.family_key, len(effects), len(added))
    return summary

"""Named CLHEAR releases for app consumers.

A release is an immutable pointer to an L1 snapshot plus a manifest that
declares which layers are present. L2–L8 are listed as reserved until published.

Storage:
  s3://…/releases/latest.json
  s3://…/releases/{id}/manifest.json
  s3://…/releases/{id}/l1/snapshot.db

When CLHEAR_RELEASES_S3_PREFIX is empty, the same layout is written under
settings.clhear_artifacts_dir / "releases".
"""
from __future__ import annotations

import hashlib
import json
import os
import shutil
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from app.clhear.layers import PUBLISHED_LAYERS
from app.clhear.settings import get_settings

MANIFEST_NAME = "manifest.json"
LATEST_NAME = "latest.json"


def _now() -> datetime:
    return datetime.now(timezone.utc)


def release_id_for(when: datetime | None = None) -> str:
    return "clhear-v" + (when or _now()).strftime("%Y%m%d")


def _local_root() -> Path:
    settings = get_settings()
    root = Path(settings.clhear_artifacts_dir) / "releases"
    root.mkdir(parents=True, exist_ok=True)
    return root


def _s3_parts() -> tuple[str, str] | None:
    prefix = (get_settings().clhear_releases_s3_prefix or "").strip()
    if not prefix.startswith("s3://"):
        return None
    rest = prefix[len("s3://") :].rstrip("/")
    bucket, _, key = rest.partition("/")
    return bucket, key


def _put_json_local(path: Path, payload: dict) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps(payload, indent=2, default=str))


def _get_json_local(path: Path) -> dict | None:
    if not path.exists():
        return None
    return json.loads(path.read_text())


def _s3():
    import boto3

    return boto3.client("s3", region_name=get_settings().aws_region)


def _put_json_s3(bucket: str, key: str, payload: dict) -> None:
    _s3().put_object(
        Bucket=bucket,
        Key=key,
        Body=json.dumps(payload, indent=2, default=str).encode("utf-8"),
        ContentType="application/json",
    )


def _get_json_s3(bucket: str, key: str) -> dict | None:
    try:
        body = _s3().get_object(Bucket=bucket, Key=key)["Body"].read()
    except Exception:
        return None
    return json.loads(body)


def _copy_snapshot_s3(src_uri: str, dest_bucket: str, dest_key: str) -> None:
    src_bucket, src_key = src_uri[len("s3://") :].split("/", 1)
    _s3().copy_object(
        Bucket=dest_bucket,
        Key=dest_key,
        CopySource={"Bucket": src_bucket, "Key": src_key},
    )


def _sha256_file(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1024 * 1024), b""):
            h.update(chunk)
    return h.hexdigest()


def _write_reserved_prefixes(release_id: str) -> None:
    """Keep l2/–l8/ prefixes present so later layers attach without a new layout."""
    marker = {
        "layer_status": "not_published",
        "detail": "Reserved. CLHEAR publishes L0+L1 only.",
    }
    s3 = _s3_parts()
    if s3:
        bucket, prefix = s3
        for n in range(2, 9):
            key = f"{prefix}/{release_id}/l{n}/.reserved".lstrip("/")
            try:
                _s3().put_object(
                    Bucket=bucket,
                    Key=key,
                    Body=json.dumps(marker).encode("utf-8"),
                    ContentType="application/json",
                )
            except Exception:
                break
        return
    root = _local_root()
    for n in range(2, 9):
        path = root / release_id / f"l{n}" / ".reserved"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(json.dumps(marker, indent=2))


def corpus_counts(engine) -> dict[str, int]:
    import sqlalchemy as sa

    from app.clhear.l1.models import change_events, clauses, source_families, sources

    with engine.connect() as conn:
        def _count(table) -> int:
            try:
                return int(conn.execute(sa.select(sa.func.count()).select_from(table)).scalar() or 0)
            except Exception:
                return 0

        return {
            "families": _count(source_families),
            "sources": _count(sources),
            "clauses": _count(clauses),
            "change_events": _count(change_events),
        }


def build_manifest(
    *,
    release_id: str,
    snapshot_uri: str,
    content_hash: str,
    counts: dict[str, int],
    generated_at: datetime | None = None,
) -> dict[str, Any]:
    when = generated_at or _now()
    return {
        "id": release_id,
        "generated_at": when.isoformat(),
        "layers": list(PUBLISHED_LAYERS),
        "reserved_layers": ["L2", "L3", "L4", "L5", "L6", "L7", "L8"],
        "spec_version": 1,
        "l1": {
            "snapshot_uri": snapshot_uri,
            "content_hash": content_hash,
            "counts": counts,
        },
    }


def publish_release(
    engine,
    *,
    snapshot_path: str | None = None,
    snapshot_uri: str | None = None,
    release_id: str | None = None,
) -> dict:
    """Write an immutable named release from the current L1 snapshot."""
    rid = release_id or release_id_for()
    settings = get_settings()
    counts = corpus_counts(engine)
    content_hash = ""
    dest_uri = ""

    src_path = Path(snapshot_path) if snapshot_path else None
    if src_path and src_path.exists():
        content_hash = _sha256_file(src_path)

    _write_reserved_prefixes(rid)

    s3 = _s3_parts()
    if s3:
        bucket, prefix = s3
        dest_key = f"{prefix}/{rid}/l1/snapshot.db".lstrip("/")
        if snapshot_uri and snapshot_uri.startswith("s3://"):
            _copy_snapshot_s3(snapshot_uri, bucket, dest_key)
        elif src_path and src_path.exists():
            _s3().upload_file(str(src_path), bucket, dest_key)
        dest_uri = f"s3://{bucket}/{dest_key}"
        manifest = build_manifest(release_id=rid, snapshot_uri=dest_uri, content_hash=content_hash, counts=counts)
        _put_json_s3(bucket, f"{prefix}/{rid}/{MANIFEST_NAME}".lstrip("/"), manifest)
        _put_json_s3(bucket, f"{prefix}/{LATEST_NAME}".lstrip("/"), {"id": rid, "manifest_uri": f"s3://{bucket}/{prefix}/{rid}/{MANIFEST_NAME}"})
        return manifest

    root = _local_root()
    dest = root / rid / "l1" / "snapshot.db"
    dest.parent.mkdir(parents=True, exist_ok=True)
    if src_path and src_path.exists():
        shutil.copy2(src_path, dest)
        content_hash = content_hash or _sha256_file(dest)
        dest_uri = dest.resolve().as_uri()
    elif settings.database_url.startswith("sqlite:///"):
        db_path = settings.database_url.replace("sqlite:///", "", 1)
        if os.path.exists(db_path):
            shutil.copy2(db_path, dest)
            content_hash = _sha256_file(dest)
            dest_uri = dest.resolve().as_uri()
    manifest = build_manifest(release_id=rid, snapshot_uri=dest_uri, content_hash=content_hash, counts=counts)
    _put_json_local(root / rid / MANIFEST_NAME, manifest)
    _put_json_local(root / LATEST_NAME, {"id": rid})
    return manifest


def _live_manifest(engine) -> dict:
    return build_manifest(
        release_id="clhear-vLIVE",
        snapshot_uri=get_settings().database_url,
        content_hash="",
        counts=corpus_counts(engine),
    )


def get_latest(engine=None) -> dict | None:
    s3 = _s3_parts()
    if s3:
        bucket, prefix = s3
        pointer = _get_json_s3(bucket, f"{prefix}/{LATEST_NAME}".lstrip("/"))
        if not pointer:
            return _live_manifest(engine) if engine is not None else None
        mid = pointer.get("id")
        return get_release(mid, engine=engine) if mid else None
    pointer = _get_json_local(_local_root() / LATEST_NAME)
    if pointer and pointer.get("id"):
        return get_release(pointer["id"], engine=engine)
    if engine is not None:
        return _live_manifest(engine)
    return None


def get_release(release_id: str, engine=None) -> dict | None:
    if release_id in {"latest", "clhear-vLATEST"}:
        return get_latest(engine)
    s3 = _s3_parts()
    if s3:
        bucket, prefix = s3
        return _get_json_s3(bucket, f"{prefix}/{release_id}/{MANIFEST_NAME}".lstrip("/"))
    return _get_json_local(_local_root() / release_id / MANIFEST_NAME) or (
        _live_manifest(engine) if engine is not None and release_id == "clhear-vLIVE" else None
    )


def list_releases(engine=None) -> list[dict]:
    found: list[dict] = []
    s3 = _s3_parts()
    if s3:
        bucket, prefix = s3
        resp = _s3().list_objects_v2(Bucket=bucket, Prefix=f"{prefix}/".lstrip("/"), Delimiter="/")
        for cp in resp.get("CommonPrefixes") or []:
            rid = cp.get("Prefix", "").rstrip("/").split("/")[-1]
            if rid.startswith("clhear-v"):
                man = get_release(rid, engine=engine)
                if man:
                    found.append(man)
    else:
        root = _local_root()
        for child in sorted(root.iterdir()) if root.exists() else []:
            if child.is_dir() and child.name.startswith("clhear-v"):
                man = get_release(child.name, engine=engine)
                if man:
                    found.append(man)
    if not found and engine is not None:
        found.append(_live_manifest(engine))
    found.sort(key=lambda m: m.get("generated_at") or "", reverse=True)
    return found


def pin_release(release_id: str, engine=None) -> dict:
    man = get_release(release_id, engine=engine)
    if not man:
        raise ValueError(f"release {release_id} not found")
    pointer = {"id": release_id, "pinned": True}
    s3 = _s3_parts()
    if s3:
        bucket, prefix = s3
        _put_json_s3(bucket, f"{prefix}/pinned.json".lstrip("/"), pointer)
    else:
        _put_json_local(_local_root() / "pinned.json", pointer)
    man = dict(man)
    man["pinned"] = True
    return man

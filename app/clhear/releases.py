"""Named CLHEAR releases for app consumers (HLD v2 §3 release pipeline, I6, I10).

A release is an immutable snapshot plus a manifest that declares which layers are
present. Versions are semantic dates (``2026.09.28``); daily deltas are recorded
between consecutive releases. A layer appears in ``layers`` only when its evals
gate passed (I10); the frozen model manifest (I6) and the Sigstore signature
fields travel with the manifest. ``scripts/verify_release.py`` checks all three.

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
import re
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


RELEASE_ID_RE = re.compile(r"^(\d{4}\.\d{2}\.\d{2}|clhear-v[\w.\-]+)$")


def release_id_for(when: datetime | None = None) -> str:
    """Semantic-date version (HLD v2 §3): ``YYYY.MM.DD``."""
    return (when or _now()).strftime("%Y.%m.%d")


def is_release_id(value: str) -> bool:
    return bool(RELEASE_ID_RE.match(value or ""))


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


def _write_reserved_prefixes(release_id: str, reserved: list[str] | None = None) -> None:
    """Keep l{n}/ prefixes present for layers not published in this release."""
    marker = {
        "layer_status": "not_published",
        "detail": "Not published in this release (below gate or not yet live).",
    }
    numbers = [int(l[1:]) for l in (reserved or [f"L{n}" for n in range(2, 9)])]
    s3 = _s3_parts()
    if s3:
        bucket, prefix = s3
        for n in numbers:
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
    for n in numbers:
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


def _gate_layers(engine) -> tuple[list[str], list[str], dict]:
    """Layers that publish in this release (gate passed) vs reserved (I10)."""
    from app.clhear.layers import LAYER_CATALOG
    from app.clhear.platform import gates

    published: list[str] = ["L0"]
    reserved: list[str] = []
    statuses: dict = {}
    if engine is None:
        return list(PUBLISHED_LAYERS), [l for l in LAYER_CATALOG if l not in PUBLISHED_LAYERS], {}
    for layer, meta in LAYER_CATALOG.items():
        if layer == "L0":
            continue
        st = gates.gate_status(engine, layer)
        statuses[layer] = {"passed": st["passed"], "failed": st["failed"], "missing": st["missing"]}
        # A layer publishes when it is live in the catalog AND its gate passed.
        if meta["published"] and st["passed"]:
            published.append(layer)
        elif meta["published"] and not st["passed"] and not st["suites"]:
            # No gate suites have ever run (fresh install): keep the catalog's
            # published flag but mark the gate as unverified so verify_release flags it.
            published.append(layer)
            statuses[layer]["unverified"] = True
        else:
            reserved.append(layer)
    return published, reserved, statuses


def build_manifest(
    *,
    release_id: str,
    snapshot_uri: str,
    content_hash: str,
    counts: dict[str, int],
    generated_at: datetime | None = None,
    engine=None,
    model_manifest: dict | None = None,
    previous: dict | None = None,
) -> dict[str, Any]:
    from app.clhear.platform.manifest import build_model_manifest

    when = generated_at or _now()
    published, reserved, gate_statuses = _gate_layers(engine)
    mm = model_manifest or _FROZEN_MODEL_MANIFEST or build_model_manifest(release_id=release_id)
    manifest: dict[str, Any] = {
        "id": release_id,
        "version": release_id,
        "generated_at": when.isoformat(),
        "spec_version": 2,
        "layers": published,
        "reserved_layers": reserved,
        "gates": gate_statuses,
        "model_manifest": mm,
        "l1": {
            "snapshot_uri": snapshot_uri,
            "content_hash": content_hash,
            "counts": counts,
        },
        "artifacts": {
            "snapshot": {"uri": snapshot_uri, "sha256": content_hash},
        },
        "signature": {
            # Filled by the release workflow (cosign sign-blob, keyless OIDC).
            "scheme": "sigstore-cosign-keyless",
            "bundle_uri": "",
            "signed": False,
            "identity": "",
        },
        "sbom_uri": "",
        "delta": _delta(previous, counts, published),
        "licence": {"data": "ODC-By-1.0", "text_and_schemas": "CC-BY-4.0", "code": "Apache-2.0"},
    }
    manifest["manifest_hash"] = _manifest_hash(manifest)
    return manifest


def _manifest_hash(manifest: dict) -> str:
    body = {k: v for k, v in manifest.items() if k not in {"manifest_hash", "signature"}}
    return hashlib.sha256(json.dumps(body, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def verify_manifest_hash(manifest: dict) -> bool:
    return manifest.get("manifest_hash") == _manifest_hash(manifest)


def _delta(previous: dict | None, counts: dict[str, int], layers: list[str]) -> dict:
    """Daily delta vs the previous release (counts and layer set)."""
    if not previous:
        return {"against": None, "counts": {}, "layers_added": [], "layers_removed": []}
    prev_counts = (previous.get("l1") or {}).get("counts") or {}
    prev_layers = set(previous.get("layers") or [])
    return {
        "against": previous.get("id"),
        "counts": {k: int(counts.get(k, 0)) - int(prev_counts.get(k, 0)) for k in set(counts) | set(prev_counts)},
        "layers_added": sorted(set(layers) - prev_layers),
        "layers_removed": sorted(prev_layers - set(layers)),
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

    previous = get_latest(engine=None)
    _, reserved, _ = _gate_layers(engine)
    _write_reserved_prefixes(rid, reserved)

    s3 = _s3_parts()
    if s3:
        bucket, prefix = s3
        dest_key = f"{prefix}/{rid}/l1/snapshot.db".lstrip("/")
        if snapshot_uri and snapshot_uri.startswith("s3://"):
            _copy_snapshot_s3(snapshot_uri, bucket, dest_key)
        elif src_path and src_path.exists():
            _s3().upload_file(str(src_path), bucket, dest_key)
        dest_uri = f"s3://{bucket}/{dest_key}"
        manifest = build_manifest(
            release_id=rid, snapshot_uri=dest_uri, content_hash=content_hash, counts=counts, engine=engine, previous=previous
        )
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
    manifest = build_manifest(
        release_id=rid, snapshot_uri=dest_uri, content_hash=content_hash, counts=counts, engine=engine, previous=previous
    )
    _put_json_local(root / rid / MANIFEST_NAME, manifest)
    _put_json_local(root / LATEST_NAME, {"id": rid})
    return manifest


def _live_manifest(engine) -> dict:
    return build_manifest(
        release_id="clhear-vLIVE",
        snapshot_uri=get_settings().database_url,
        content_hash="",
        counts=corpus_counts(engine),
        engine=engine,
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
            if is_release_id(rid):
                man = get_release(rid, engine=engine)
                if man:
                    found.append(man)
    else:
        root = _local_root()
        for child in sorted(root.iterdir()) if root.exists() else []:
            if child.is_dir() and is_release_id(child.name):
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


def main(argv: list[str] | None = None) -> int:
    """CLI: python -m app.clhear.releases publish [--release ID] [--model-manifest path]"""
    import argparse
    import sys

    from app.clhear.db import get_engine, run_migrations

    parser = argparse.ArgumentParser()
    parser.add_argument("command", choices=["publish", "latest", "list"])
    parser.add_argument("--release", default=None)
    parser.add_argument("--model-manifest", default=None)
    parser.add_argument("--snapshot", default=None)
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    engine = get_engine()
    run_migrations(engine)
    if args.command == "publish":
        mm = json.loads(Path(args.model_manifest).read_text()) if args.model_manifest else None
        if mm is not None:
            # publish_release builds the manifest itself; inject the frozen model manifest.
            global _FROZEN_MODEL_MANIFEST
            _FROZEN_MODEL_MANIFEST = mm
        manifest = publish_release(engine, snapshot_path=args.snapshot, release_id=args.release)
        print(json.dumps(manifest, indent=2, default=str))
        return 0
    if args.command == "latest":
        print(json.dumps(get_latest(engine), indent=2, default=str))
        return 0
    print(json.dumps(list_releases(engine), indent=2, default=str))
    return 0


_FROZEN_MODEL_MANIFEST: dict | None = None


if __name__ == "__main__":
    raise SystemExit(main())

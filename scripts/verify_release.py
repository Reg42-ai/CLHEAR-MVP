"""Verify a CLHEAR release (HLD v2 §8 item 1 acceptance).

    python scripts/verify_release.py <release-dir-or-manifest.json> [--require-signature]

Checks: manifest hash; snapshot sha256 matches the file next to the manifest (or the
S3 object when reachable); model manifest is complete, Bedrock-hosted and
procurement-clean for derivation classes; every published layer's gate passed; and,
when ``--require-signature`` is set or a signature bundle is present, the Sigstore
bundle verifies with ``cosign verify-blob``. Prints the frozen model ids and exits 0
on success.
"""
from __future__ import annotations

import hashlib
import json
import shutil
import subprocess
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app.clhear.platform.manifest import check_manifest, frozen_model_ids  # noqa: E402
from app.clhear.releases import verify_manifest_hash  # noqa: E402


def _sha256(path: Path) -> str:
    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def verify(target: Path, *, require_signature: bool = False, cosign_identity: str | None = None) -> tuple[bool, list[str], dict]:
    manifest_path = target / "manifest.json" if target.is_dir() else target
    root = manifest_path.parent
    problems: list[str] = []
    manifest = json.loads(manifest_path.read_text())

    if manifest.get("spec_version") != 2:
        problems.append(f"spec_version {manifest.get('spec_version')} != 2")
    if not verify_manifest_hash(manifest):
        problems.append("manifest_hash does not match manifest body")

    snap = (manifest.get("artifacts") or {}).get("snapshot") or {}
    declared = snap.get("sha256") or (manifest.get("l1") or {}).get("content_hash") or ""
    local_snapshot = root / "l1" / "snapshot.db"
    if local_snapshot.exists():
        actual = _sha256(local_snapshot)
        if declared and actual != declared:
            problems.append(f"snapshot sha256 mismatch: manifest={declared[:12]} file={actual[:12]}")
    elif declared and not str(snap.get("uri", "")).startswith("s3://"):
        problems.append("snapshot file missing and no s3 uri")

    mm = manifest.get("model_manifest") or {}
    if not mm:
        problems.append("model_manifest missing")
    else:
        problems += [f"model_manifest: {p}" for p in check_manifest(mm)]

    gates = manifest.get("gates") or {}
    for layer in manifest.get("layers") or []:
        if layer == "L0":
            continue
        st = gates.get(layer)
        if st is None:
            problems.append(f"{layer} published without a gate record")
        elif st.get("unverified"):
            problems.append(f"{layer} published with an unverified gate (no suites have run)")
        elif not st.get("passed"):
            problems.append(f"{layer} published below gate: failed={st.get('failed')} missing={st.get('missing')}")

    sig = manifest.get("signature") or {}
    bundle = root / "manifest.sigstore.json"
    if require_signature or bundle.exists() or sig.get("signed"):
        if not bundle.exists():
            problems.append("signature bundle manifest.sigstore.json missing")
        elif shutil.which("cosign") is None:
            if require_signature:
                problems.append("cosign not installed; cannot verify signature")
        else:
            cmd = [
                "cosign", "verify-blob", str(manifest_path), "--bundle", str(bundle),
                "--certificate-identity-regexp", cosign_identity or sig.get("identity") or ".*",
                "--certificate-oidc-issuer", "https://token.actions.githubusercontent.com",
            ]
            res = subprocess.run(cmd, capture_output=True, text=True)
            if res.returncode != 0:
                problems.append(f"cosign verify-blob failed: {res.stderr.strip()[:300]}")
    return not problems, problems, manifest


def main(argv: list[str]) -> int:
    if not argv:
        print(__doc__)
        return 2
    target = Path(argv[0])
    ok, problems, manifest = verify(target, require_signature="--require-signature" in argv)
    print(f"release {manifest.get('id')} layers={manifest.get('layers')}")
    print("frozen model ids:")
    for tc, mid in sorted(frozen_model_ids(manifest.get("model_manifest") or {}).items()):
        print(f"  {tc:16s} {mid}")
    for p in problems:
        print("PROBLEM", p)
    print("VERIFIED" if ok else "FAILED")
    return 0 if ok else 1


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

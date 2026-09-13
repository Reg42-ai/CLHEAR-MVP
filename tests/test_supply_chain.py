"""Supply chain (HLD v2 §7.1; item 17): every requirement is pinned exactly in
requirements.lock and the pin satisfies the stated minimum; CI and the release job
install the lock; every release ships an SBOM and a Sigstore-signed manifest."""
from __future__ import annotations

import re
from pathlib import Path

import pytest
from packaging.requirements import Requirement
from packaging.version import Version

ROOT = Path(__file__).resolve().parents[1]


def _requirements() -> list[Requirement]:
    out = []
    for line in (ROOT / "requirements.txt").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            out.append(Requirement(line))
    return out


def _lock() -> dict[str, str]:
    pins: dict[str, str] = {}
    for line in (ROOT / "requirements.lock").read_text().splitlines():
        line = line.split("#", 1)[0].strip()
        if not line:
            continue
        m = re.fullmatch(r"([A-Za-z0-9_.\-]+)(\[[^\]]+\])?==([A-Za-z0-9_.!+\-]+)", line)
        assert m, f"lock line is not an exact pin: {line!r}"
        pins[m.group(1).lower().replace("_", "-")] = m.group(3)
    return pins


def test_every_requirement_is_pinned_exactly_and_within_its_minimum():
    pins = _lock()
    assert len(pins) >= 30  # transitive closure, not just the top level
    for req in _requirements():
        name = req.name.lower().replace("_", "-")
        assert name in pins, f"{req.name} has no pin in requirements.lock"
        assert req.specifier.contains(Version(pins[name]), prereleases=False), f"{req.name}=={pins[name]} violates {req.specifier}"
        for extra in req.extras:  # psycopg[binary], PyJWT[crypto] pull their own packages
            dep = {"binary": "psycopg-binary", "crypto": "cryptography"}.get(extra)
            if dep:
                assert dep in pins, f"extra {req.name}[{extra}] needs {dep} pinned"


def test_ci_and_release_install_the_lock_and_release_ships_sbom_and_signature():
    ci = (ROOT / ".github/workflows/ci.yml").read_text()
    rel = (ROOT / ".github/workflows/release.yml").read_text()
    assert "pip install -r requirements.lock" in ci and "pip install -r requirements.txt" not in ci.replace("requirements.lock", "")
    assert "pip install -r requirements.lock" in rel
    assert "anchore/sbom-action" in rel and "sbom.spdx.json" in rel
    assert "sigstore/cosign-installer" in rel and "cosign sign-blob" in rel
    assert "agnostic_scan" in rel  # I5 on every release
    verify = (ROOT / "scripts/verify_release.py").read_text()
    assert "verify-blob" in verify or "verify_blob" in verify


def test_lock_header_documents_regeneration():
    head = (ROOT / "requirements.lock").read_text().splitlines()[:8]
    assert any("pip freeze" in l for l in head) and any("Python 3.12" in l for l in head)


@pytest.mark.parametrize("path", ["requirements.txt", "requirements.lock"])
def test_no_git_or_url_requirements(path: str):
    text = (ROOT / path).read_text()
    assert "git+" not in text and "http://" not in text and "https://" not in text.replace("# ", "")  # no unpinnable sources

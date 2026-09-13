"""HLD v2 §6 / §10 — the public `clhear` repository layout.

The exporter compiles the whole public repo locally on every release: README,
licences, governance artefacts, the standard, generated JSON Schemas, the vault
(allow-list: statements only under a republishable rights basis), evals with
the stdlib harness, SDKs and release notes with contributor attribution. It
pushes nothing unless the public disclosure is confirmed (§9 never-list).
"""
from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path

import pytest
import sqlalchemy as sa

from app.clhear.derived_models import asserts, obligations
from app.clhear.l1.models import clauses, family_members, source_families, source_versions, sources
from app.clhear.platform import contributions as c
from app.clhear.platform import exporter, public_repo
from app.clhear.platform.evals import run_all

RELEASE = "clhear-v0.13.0-test"
PUBLIC_TEXT = ("A relevant person must apply customer due diligence measures if the person establishes a "
               "business relationship or carries out an occasional transaction exceeding the threshold.")
RESTRICTED_TEXT = "RESTRICTED-VERBATIM-MARKER the organization shall determine the boundaries of its management system"
OB_PUBLIC, OB_RESTRICTED = "OBL:uksi/2017/692#regulation-27", "OBL:iso/27001-2022#clause-4.3"


def _seed(engine):
    with engine.begin() as conn:
        fam = conn.execute(source_families.insert().values(key="t", name="T", scope_charter={})).inserted_primary_key[0]

        def add(key, name, jur, license, rights):
            sid = conn.execute(sources.insert().values(family_id=fam, key=key, name=name, kind="regulation", license=license,
                                                       short_name=name[:12], jurisdiction=jur, topics=[], rights_basis=rights)
                               ).inserted_primary_key[0]
            conn.execute(family_members.insert().values(family_id=fam, source_id=sid, relation="root", tier="binding",
                                                        status="active", added_via="manual"))
            return conn.execute(source_versions.insert().values(source_id=sid, version_label="c", version_kind="consolidated",
                                                                content_hash=f"sha:{key}", s3_uri=f"s3://x/{key}",
                                                                status="in_force")).inserted_primary_key[0]

        uk = add("uksi/2017/692", "UK MLRs", "UK", "open", "open_licence")
        iso = add("iso/27001-2022", "ISO 27001", "International", "restricted", "licensed")
        cid_uk = conn.execute(clauses.insert().values(source_version_id=uk, ref="regulation-27", path="r27", ordering=27,
                                                      text=PUBLIC_TEXT, text_hash="h27", public_ok=True)).inserted_primary_key[0]
        cid_iso = conn.execute(clauses.insert().values(source_version_id=iso, ref="clause-4.3", path="c43", ordering=43,
                                                       text=RESTRICTED_TEXT, text_hash="hiso", public_ok=False,
                                                       span_start=0, span_end=len(RESTRICTED_TEXT))).inserted_primary_key[0]
        for oid, key, ref, cid, statement, jur, h in (
            (OB_PUBLIC, "uksi/2017/692", "regulation-27", cid_uk, PUBLIC_TEXT, "UK", "h27"),
            (OB_RESTRICTED, "iso/27001-2022", "clause-4.3", cid_iso, RESTRICTED_TEXT, "International", "hiso"),
        ):
            conn.execute(obligations.insert().values(id=oid, source_key=key, clause_ref=ref, title="Duty " + ref,
                                                     statement=statement, modality="must", jurisdiction=jur, confidence=0.9,
                                                     status="derived", text_hash=h))
            conn.execute(asserts.insert().values(id=f"AST-{ref}", obligation_id=oid, clause_id=cid, source_key=key,
                                                 clause_ref=ref, strength="explicit", text_hash=h))


def _export(engine, tmp_path, **kw):
    from app.clhear import curated

    curated.seed(engine)
    run_all(engine, release=RELEASE)
    out = tmp_path / "public-repo"
    return out, exporter.export_release(engine, RELEASE, repo_dir=out, **kw)


def _tree(root: Path) -> list[str]:
    return sorted(str(p.relative_to(root)) for p in root.rglob("*") if p.is_file())


# --------------------------------------------------------------------------- layout


def test_public_repo_layout_and_governance_artefacts(engine, tmp_path):
    _seed(engine)
    out, result = _export(engine, tmp_path)
    files = _tree(out)
    for must in ("README.md", "ROADMAP.md", "CONTRIBUTING.md", "LICENSES/Apache-2.0.txt", "LICENSES/CC-BY-4.0.txt",
                 "LICENSES/ODC-By-1.0.txt", "LICENSES/README.md", "governance/CHARTER.md", "governance/CLA.md",
                 "governance/CODE_OF_CONDUCT.md", "governance/RELEASE_POLICY.md", "governance/DEPRECATION_POLICY.md",
                 "governance/CONFLICT_OF_INTEREST.md", "governance/STEERING_VOTING.md", "governance/WORKING_GROUPS.md",
                 "governance/TRADEMARK_POLICY.md", "standard/CLHEAR-STANDARD.md", "schema/index.json",
                 f"snapshots/{RELEASE}/l1/snapshot.json", f"evals/{RELEASE}.json", "evals/harness.py", "evals/README.md",
                 "sdks/python/clhear/__init__.py", "sdks/typescript/src/index.ts", f"RELEASE_NOTES/{RELEASE}.md",
                 f"vault/{RELEASE}/index.md", ".github/PULL_REQUEST_TEMPLATE.md", ".github/DISCUSSION_TEMPLATE/rfc.yml"):
        assert must in files, must
    # licence texts are the real ones, not summaries
    assert "Apache License" in (out / "LICENSES/Apache-2.0.txt").read_text() and "Version 2.0" in (out / "LICENSES/Apache-2.0.txt").read_text()
    assert "Attribution 4.0 International" in (out / "LICENSES/CC-BY-4.0.txt").read_text()
    assert "Open Data Commons Attribution License" in (out / "LICENSES/ODC-By-1.0.txt").read_text()
    charter = (out / "governance/CHARTER.md").read_text()
    for section in ("Purpose", "Principles", "Roles", "Steering", "Working groups", "Amendments"):
        assert section in charter, section
    cla = (out / "governance/CLA.md").read_text()
    assert "patent" in cla.lower() and "standard" in cla.lower()
    readme = (out / "README.md").read_text()
    assert "clhear" in readme.lower() and "vault/" in readme and "schema/" in readme and "evals/" in readme
    assert result["release"] == RELEASE and len(result["files"]) == len(set(result["files"]))
    assert not (out / ".git").exists()  # compiled locally, never pushed here


def test_schemas_are_valid_json_schema_per_layer_table(engine, tmp_path):
    _seed(engine)
    out, _ = _export(engine, tmp_path)
    index = json.loads((out / "schema/index.json").read_text())
    assert {"L1", "L2", "L3", "L4", "L5", "L6", "L7"} <= set(index["layers"])
    n = 0
    for layer, rels in index["layers"].items():
        for rel in rels:
            doc = json.loads((out / "schema" / rel).read_text())
            assert doc["$schema"].startswith("https://json-schema.org/draft/2020-12")
            assert doc["type"] == "object" and doc["properties"] and doc["x-clhear"]["layer"] == layer
            assert set(doc["required"]) <= set(doc["properties"])
            n += 1
    assert n >= 20
    ob = json.loads((out / "schema/l2/obligations.schema.json").read_text())
    assert {"id", "valid_from", "valid_to", "why_trail_id", "version"} <= set(ob["properties"])
    assert ob["x-clhear"]["primary_key"] == ["id"] or "id" in ob["x-clhear"]["primary_key"]


# --------------------------------------------------------------------------- vault + rights (I8)


def test_vault_withholds_text_without_a_republication_basis(engine, tmp_path):
    _seed(engine)
    out, _ = _export(engine, tmp_path)
    vault = out / "vault" / RELEASE
    notes = {p.name: p.read_text() for p in (vault / "L2").glob("*.md")}
    assert len(notes) == 2
    public = next(t for n, t in notes.items() if "uksi" in n)
    restricted = next(t for n, t in notes.items() if "iso" in n)
    assert PUBLIC_TEXT in public and "text_public: true" in public
    assert "Text withheld" in restricted and "text_public: false" in restricted and "hiso" in restricted
    # the restricted string appears nowhere in the whole public tree — not in notes, snapshots, evals or release notes
    leaks = [rel for rel in _tree(out) if "RESTRICTED-VERBATIM-MARKER" in (out / rel).read_text(errors="ignore")]
    assert leaks == []
    index = (vault / "index.md").read_text()
    assert "L2" in index and "L3" in index and "L5" in index
    blocks = list((vault / "L3").glob("*.md"))
    assert blocks and all("BLK-" in p.name for p in blocks)


# --------------------------------------------------------------------------- evals harness + contributed cases


def test_harness_runs_on_a_golden_file_and_contributed_cases_ship(engine, tmp_path):
    _seed(engine)
    c.sign_cla(engine, "ada@example.com")
    row = c.submit(engine, contributor_email="ada@example.com", kind="golden_case",
                   proposed={"suite": "l2_precision", "case": {"id": "uk-r27-action", "expected": "apply customer due diligence measures"}},
                   rationale="a clear action for the deterministic reader")
    assert row["status"] == "checked"
    c.review(engine, row["id"], reviewer_email="avner@reg42.ai", decision="accept")
    assert c.review(engine, row["id"], reviewer_email="maintainer@reg42.ai", decision="accept")["status"] == "accepted"
    bad = c.submit(engine, contributor_email="ada@example.com", kind="golden_case", proposed={"suite": "x", "case": {"no": "id"}})
    assert bad["status"] == "checks_failed" and "case.id" in bad["checks"][0]["detail"]

    out, _ = _export(engine, tmp_path)
    contributed = json.loads((out / "evals/contributed/l2_precision.json").read_text())
    assert contributed["cases"][0]["id"] == "uk-r27-action" and contributed["cases"][0]["contributed_by"] == "ada"
    assert contributed["cases"][0]["contribution"] == row["id"]

    harness = out / "evals/harness.py"
    preds = tmp_path / "preds.json"
    preds.write_text(json.dumps({"uk-r27-action": "apply customer due diligence measures"}))
    ok = subprocess.run([sys.executable, str(harness), "--golden", str(out / "evals/contributed/l2_precision.json"),
                         "--predictions", str(preds), "--json"], capture_output=True, text=True)
    assert ok.returncode == 0, ok.stdout + ok.stderr
    report = json.loads(ok.stdout)
    assert report["gate"] == "pass" and report["passed"] == 1
    preds.write_text(json.dumps({"uk-r27-action": "something else"}))
    fail = subprocess.run([sys.executable, str(harness), "--golden", str(out / "evals/contributed/l2_precision.json"),
                           "--predictions", str(preds)], capture_output=True, text=True)
    assert fail.returncode == 1 and "FAIL" in fail.stdout
    validate = subprocess.run([sys.executable, str(harness), "--validate", str(out / "evals")], capture_output=True, text=True)
    assert validate.returncode == 0, validate.stdout


# --------------------------------------------------------------------------- release notes + attribution


def test_release_notes_carry_attribution_and_impact(engine, tmp_path):
    _seed(engine)
    c.sign_cla(engine, "ada@example.com")
    row = c.submit(engine, contributor_email="ada@example.com", kind="correction", target_ref=OB_PUBLIC, field="title",
                   proposed={"value": "Apply customer due diligence measures"}, rationale="heading")
    c.review(engine, row["id"], reviewer_email="avner@reg42.ai", decision="accept")
    c.review(engine, row["id"], reviewer_email="maintainer@reg42.ai", decision="accept")
    shipped = c.release_contributions(engine, RELEASE)
    assert shipped and shipped[0]["impact"]["obligations_changed"] == 1
    out, _ = _export(engine, tmp_path)
    notes = (out / "RELEASE_NOTES" / f"{RELEASE}.md").read_text()
    assert row["id"] in notes and "ada" in notes and "1 obligation" in notes
    assert "ada@example.com" not in notes  # handles, never emails
    vault_note = next((out / "vault" / RELEASE / "L2").glob("*uksi*")).read_text()
    assert "Apply customer due diligence measures" in vault_note


# --------------------------------------------------------------------------- disclosure gate (§9)


def test_push_refused_until_disclosure_confirmed(engine, tmp_path, monkeypatch):
    from app.clhear.settings import get_settings

    _seed(engine)
    from app.clhear import curated

    curated.seed(engine)
    run_all(engine, release=RELEASE)
    monkeypatch.setenv("CLHEAR_PUBLIC_REPO_URL", "https://github.com/example/clhear.git")
    monkeypatch.delenv("CLHEAR_PUBLIC_DISCLOSURE_CONFIRMED", raising=False)
    get_settings.cache_clear()
    assert exporter.disclosure_confirmed() is False
    with pytest.raises(exporter.DisclosureNotConfirmed):
        exporter.export_release(engine, RELEASE, repo_dir=tmp_path / "p", push=True)
    assert not (tmp_path / "p" / ".git").exists()
    # the local layout is still compiled without --push
    result = exporter.export_release(engine, RELEASE, repo_dir=tmp_path / "p", push=False)
    assert (tmp_path / "p" / "README.md").exists() and result["files"]
    get_settings.cache_clear()


def test_public_repo_build_is_deterministic_for_static_parts(engine, tmp_path):
    _seed(engine)
    out1, _ = _export(engine, tmp_path / "a")
    out2, _ = _export(engine, tmp_path / "b")
    for rel in ("README.md", "governance/CHARTER.md", "schema/l2/obligations.schema.json", f"vault/{RELEASE}/index.md"):
        assert (out1 / rel).read_text() == (out2 / rel).read_text(), rel
    assert public_repo.STATIC_DIR.exists() and (public_repo.STATIC_DIR / "LICENSES").is_dir()

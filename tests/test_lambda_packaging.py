import importlib.util
import json
from pathlib import Path
import zipfile

import pytest


spec = importlib.util.spec_from_file_location("package_webui", Path(__file__).parents[1] / "scripts/package_webui.py")
packager = importlib.util.module_from_spec(spec)
spec.loader.exec_module(packager)


def layout(tmp_path):
    for name in ("app", "migrations", "dependencies"):
        (tmp_path / name).mkdir()
        (tmp_path / name / "__init__.py").write_text("# code\n")
    return tmp_path, tmp_path / "dependencies", tmp_path / "output/webui.zip"


def test_bundle_is_deterministic_and_excludes_workspace_data(tmp_path):
    root, deps, output = layout(tmp_path)
    (root / "deploy").mkdir()
    (root / "deploy/clhear.db").write_text("private corpus")
    (root / ".env").write_text("SECRET=not-for-the-bundle")
    first = packager.package(root, deps, output)
    first_bytes = output.read_bytes()
    (root / "app/__init__.py").touch()
    assert packager.package(root, deps, output) == first
    assert output.read_bytes() == first_bytes
    assert json.loads(output.with_suffix(".json").read_text())["kind"] == "application-code"
    with zipfile.ZipFile(output) as archive:
        assert set(archive.namelist()) == {"__init__.py", "app/__init__.py", "migrations/__init__.py"}


def test_database_in_code_tree_fails_packaging(tmp_path):
    root, deps, output = layout(tmp_path)
    (root / "app/private.sqlite").write_bytes(b"private")
    with pytest.raises(ValueError, match="Corpus databases"):
        packager.package(root, deps, output)
    assert not output.exists()


def test_dependency_cannot_shadow_application_code(tmp_path):
    root, deps, output = layout(tmp_path)
    (deps / "app").mkdir()
    (deps / "app/__init__.py").write_text("shadow")
    with pytest.raises(ValueError, match="Duplicate"):
        packager.package(root, deps, output)


def test_symlink_cannot_package_files_outside_code(tmp_path):
    root, deps, output = layout(tmp_path)
    secret = root / "secret.txt"
    secret.write_text("private")
    (root / "app/link.txt").symlink_to(secret)
    with pytest.raises(ValueError, match="Symlinks"):
        packager.package(root, deps, output)

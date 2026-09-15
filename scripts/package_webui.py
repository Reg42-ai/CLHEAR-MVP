"""Package application code and locked Lambda dependencies; never corpus data."""
from __future__ import annotations

import argparse
import hashlib
import json
from pathlib import Path
import zipfile


def package(root: Path, dependencies: Path, output: Path) -> dict:
    files = {}
    for directory, prefix in [(dependencies, ""), (root / "app", "app"), (root / "migrations", "migrations")]:
        if not directory.is_dir():
            raise ValueError(f"Missing package directory: {directory.name}")
        for path in sorted(directory.rglob("*")):
            if path.is_symlink():
                raise ValueError("Symlinks are not allowed in the Lambda bundle")
            if not path.is_file() or "__pycache__" in path.parts or path.suffix == ".pyc":
                continue
            if path.suffix.lower() in {".db", ".sqlite", ".sqlite3"}:
                raise ValueError("Corpus databases are not code deployment artifacts")
            name = (Path(prefix) / path.relative_to(directory)).as_posix()
            if name in files:
                raise ValueError(f"Duplicate bundle path: {name}")
            files[name] = path
    size = sum(path.stat().st_size for path in files.values())
    if size > 240 * 1024 * 1024:
        raise ValueError("Lambda bundle exceeds the deployment size budget")
    output.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(output, "w", compression=zipfile.ZIP_DEFLATED) as archive:
        for name, path in sorted(files.items()):
            info = zipfile.ZipInfo(name, date_time=(1980, 1, 1, 0, 0, 0))
            info.compress_type = zipfile.ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, path.read_bytes())
    result = {"sha256": hashlib.sha256(output.read_bytes()).hexdigest(),
              "uncompressed_bytes": size, "files": len(files), "kind": "application-code"}
    output.with_suffix(".json").write_text(json.dumps(result, indent=2) + "\n")
    return result


if __name__ == "__main__":
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--dependencies", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    print(json.dumps(package(Path(__file__).resolve().parents[1], args.dependencies, args.output)))

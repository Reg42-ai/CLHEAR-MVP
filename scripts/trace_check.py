"""HLD v2 requirement trace checker (docs/HLD_V2_TRACE.md).

Every row marked `done` must reference code paths and tests that exist. A row that
references a `path::test_name` must have that test function defined. Exit 1 on any
broken reference so CI keeps the trace honest. Run with `--summary` to print the
done/partial/todo/blocked counts per section.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
TRACE = ROOT / "docs" / "HLD_V2_TRACE.md"

ROW = re.compile(r"^\|\s*(CLHEAR-[\d.]+)\s*\|(.+)\|\s*(done|partial|todo|blocked)\s*\|\s*$")
PATH_TOKEN = re.compile(r"`([^`]+)`")


def _expand_braces(path: str) -> list[str]:
    m = re.search(r"\{([^}]+)\}", path)
    if not m:
        return [path]
    out = []
    for alt in m.group(1).split(","):
        out.extend(_expand_braces(path[: m.start()] + alt.strip() + path[m.end() :]))
    return out


def _check_ref(ref: str) -> str | None:
    ref = ref.strip()
    if ref in {"—", "-", "terraform validate"} or ref.startswith("CLHEAR_"):
        return None
    # `path (symbol)` -> path ; `path::test` -> path + test
    ref = re.sub(r"\s*\(.*\)$", "", ref)
    test_name = None
    if "::" in ref:
        ref, test_name = ref.split("::", 1)
    for candidate in _expand_braces(ref):
        candidate = candidate.rstrip("/")
        p = ROOT / candidate
        if candidate.endswith("*"):
            if not list((ROOT / candidate[:-1]).glob("*")):
                return f"missing glob {candidate}"
            continue
        if not p.exists():
            return f"missing path {candidate}"
        if test_name and p.is_file():
            if f"def {test_name}(" not in p.read_text(encoding="utf-8"):
                return f"missing test {test_name} in {candidate}"
    return None


def main(argv: list[str]) -> int:
    text = TRACE.read_text(encoding="utf-8")
    counts: dict[str, int] = {"done": 0, "partial": 0, "todo": 0, "blocked": 0}
    errors: list[str] = []
    for line in text.splitlines():
        m = ROW.match(line)
        if not m:
            continue
        req, body, status = m.group(1), m.group(2), m.group(3)
        counts[status] += 1
        if status != "done":
            continue
        for tok in PATH_TOKEN.findall(body):
            err = _check_ref(tok)
            if err:
                errors.append(f"{req}: {err}")
    if "--summary" in argv or errors:
        total = sum(counts.values())
        print(f"trace rows: {total} " + " ".join(f"{k}={v}" for k, v in counts.items()))
    for e in errors:
        print("TRACE ERROR", e)
    return 1 if errors else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))

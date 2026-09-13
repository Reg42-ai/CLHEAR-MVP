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


PATH_SUFFIXES = (".py", ".tf", ".md", ".yml", ".yaml", ".json", ".html", ".css", ".txt", ".lock")


def _is_path(token: str) -> bool:
    return "/" in token or token.endswith(PATH_SUFFIXES)


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


def _check_symbol(symbol: str, path_ref: str | None) -> str | None:
    """A backticked symbol following a file path must appear in that file."""
    if path_ref is None or any(ch in symbol for ch in "<>*[") or " " in symbol:
        return None
    base = re.sub(r"\s*\(.*\)$", "", path_ref).split("::", 1)[0]
    candidates = [ROOT / c for c in _expand_braces(base)]
    files = [c for c in candidates if c.is_file()]
    if not files:
        return None
    needle = symbol.split(".")[0].rstrip("-")
    for f in files:
        if needle in f.read_text(encoding="utf-8", errors="replace"):
            return None
    return f"symbol {symbol} not found in {base}"


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
        last_path: str | None = None
        for tok in PATH_TOKEN.findall(body):
            if _is_path(tok):
                err = _check_ref(tok)
                last_path = tok
            else:
                err = _check_symbol(tok, last_path)
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

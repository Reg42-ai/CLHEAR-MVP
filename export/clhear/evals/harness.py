#!/usr/bin/env python3
"""CLHEAR evals harness — score a candidate's outputs against a golden set.

Standard library only, Apache-2.0. The golden sets live in ``golden/<layer>/<suite>/*.json``
and share one shape::

    {"suite": "l7_linker", "description": "...", "cases": [{"id": "...", ..., "expected": <value>}]}

A candidate produces ``predictions.json``: ``{"<case id>": <predicted value>, ...}``.
The harness scores each case by the type of ``expected``:

* a **set-like** expected (list of ids or of ``[a, b]`` pairs) → precision / recall / F1 over the set;
* a **scalar** expected (``expected_kind``, ``expected_materiality``, ``expected`` string) → exact match;
* an **object** expected → per-key exact match, case passes when every key matches.

Usage::

    python evals/harness.py --golden evals/golden/l7/linker/core.json --predictions my_links.json
    python evals/harness.py --golden evals/golden/l2/change_events/core.json --predictions my_kinds.json --threshold 0.95
    python evals/harness.py --validate evals/golden            # check every golden file is well-formed

Exit status 0 when the suite meets the threshold (default 0.90 mean case score), 1 otherwise.
"""
from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any


def _freeze(v: Any):
    if isinstance(v, list):
        return tuple(_freeze(x) for x in v)
    if isinstance(v, dict):
        return tuple(sorted((k, _freeze(x)) for k, x in v.items()))
    return v


def _as_set(v: Any) -> set:
    if v is None:
        return set()
    if isinstance(v, (list, tuple, set)):
        return {_freeze(x) for x in v}
    return {_freeze(v)}


def expected_of(case: dict) -> tuple[str, Any]:
    """(kind, value) — the field(s) a candidate must reproduce."""
    if "expected" in case:
        return ("set" if isinstance(case["expected"], list) else "scalar"), case["expected"]
    keys = {k: v for k, v in case.items() if k.startswith("expected_")}
    if keys:
        return "object", keys
    raise ValueError(f"case {case.get('id')!r} has no expected value")


def score_case(case: dict, prediction: Any) -> dict:
    kind, expected = expected_of(case)
    if kind == "set":
        exp, got = _as_set(expected), _as_set(prediction)
        tp = len(exp & got)
        precision = tp / len(got) if got else (1.0 if not exp else 0.0)
        recall = tp / len(exp) if exp else (1.0 if not got else 0.0)
        f1 = 2 * precision * recall / (precision + recall) if (precision + recall) else 0.0
        return {"id": case.get("id"), "kind": kind, "precision": round(precision, 4), "recall": round(recall, 4),
                "f1": round(f1, 4), "score": round(f1, 4), "pass": exp == got}
    if kind == "scalar":
        ok = _freeze(prediction) == _freeze(expected)
        return {"id": case.get("id"), "kind": kind, "score": 1.0 if ok else 0.0, "pass": ok,
                "expected": expected, "got": prediction}
    pred = prediction if isinstance(prediction, dict) else {}
    hits = {k: _freeze(pred.get(k)) == _freeze(v) for k, v in expected.items()}
    score = sum(hits.values()) / len(hits) if hits else 0.0
    return {"id": case.get("id"), "kind": kind, "score": round(score, 4), "pass": all(hits.values()), "keys": hits}


def score_suite(golden: dict, predictions: dict) -> dict:
    cases = golden.get("cases") or []
    rows = [score_case(c, predictions.get(str(c.get("id")))) for c in cases]
    missing = [c.get("id") for c in cases if str(c.get("id")) not in predictions]
    n = len(rows)
    mean = sum(r["score"] for r in rows) / n if n else 0.0
    out = {"suite": golden.get("suite"), "cases": n, "passed": sum(1 for r in rows if r["pass"]),
           "mean_score": round(mean, 4), "missing_predictions": missing, "rows": rows}
    set_rows = [r for r in rows if r["kind"] == "set"]
    if set_rows:
        out["precision"] = round(sum(r["precision"] for r in set_rows) / len(set_rows), 4)
        out["recall"] = round(sum(r["recall"] for r in set_rows) / len(set_rows), 4)
    return out


def validate_golden(path: Path) -> list[str]:
    problems = []
    try:
        doc = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        return [f"{path}: unreadable ({exc})"]
    if not isinstance(doc, dict):
        return [f"{path}: not an object"]
    if "adapter" in doc and isinstance(doc.get("clauses"), list):
        # L1 boundary fixture: raw pages + the clauses a correct adapter must cut.
        bad = [c for c in doc["clauses"] if not isinstance(c, dict) or not c.get("ref")]
        return [f"{path}: boundary clause without a ref"] if bad else []
    if "release" in doc and any(k in doc for k in ("eval_scores", "suites", "scores", "runs")):
        return []  # a release's scores, written by the platform — not a golden set
    if not doc.get("suite") or not isinstance(doc.get("cases"), list):
        return [f"{path}: needs {{suite, description, cases[]}}"]
    seen = set()
    for c in doc["cases"]:
        if not isinstance(c, dict) or not c.get("id"):
            problems.append(f"{path}: a case lacks an id")
            continue
        if c["id"] in seen:
            problems.append(f"{path}: duplicate case id {c['id']!r}")
        seen.add(c["id"])
        try:
            expected_of(c)
        except ValueError as exc:
            problems.append(f"{path}: {exc}")
    return problems


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--golden", type=Path, help="golden set file")
    ap.add_argument("--predictions", type=Path, help="candidate predictions {case id: value}")
    ap.add_argument("--threshold", type=float, default=0.90, help="mean case score needed to pass (default 0.90)")
    ap.add_argument("--validate", type=Path, help="validate every golden file under this directory and exit")
    ap.add_argument("--json", action="store_true", help="print the full report as JSON")
    args = ap.parse_args(argv)

    if args.validate:
        problems = [p for f in sorted(args.validate.rglob("*.json")) for p in validate_golden(f)]
        files = len(list(args.validate.rglob("*.json")))
        print(f"{files} golden files, {len(problems)} problems")
        for p in problems:
            print(" -", p)
        return 1 if problems else 0

    if not (args.golden and args.predictions):
        ap.error("--golden and --predictions are required (or --validate)")
    golden = json.loads(args.golden.read_text(encoding="utf-8"))
    predictions = json.loads(args.predictions.read_text(encoding="utf-8"))
    report = score_suite(golden, predictions)
    report["threshold"] = args.threshold
    report["gate"] = "pass" if report["mean_score"] >= args.threshold and not report["missing_predictions"] else "fail"
    if args.json:
        print(json.dumps(report, indent=2))
    else:
        print(f"{report['suite']}: {report['passed']}/{report['cases']} cases pass, mean score {report['mean_score']:.3f}"
              + (f", precision {report['precision']:.3f} recall {report['recall']:.3f}" if "precision" in report else "")
              + f" → {report['gate'].upper()} (threshold {args.threshold})")
        if report["missing_predictions"]:
            print(f"  missing predictions for: {', '.join(map(str, report['missing_predictions']))}")
    return 0 if report["gate"] == "pass" else 1


if __name__ == "__main__":
    sys.exit(main())

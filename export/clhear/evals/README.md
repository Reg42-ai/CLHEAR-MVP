# CLHEAR evals

Evals are gates, not dashboards (HLD v2 invariant I10): a layer publishes only
when its suites pass, and every release carries its scores in
`evals/<release>.json`. This directory ships the golden sets and a
dependency-free harness so anyone can score an implementation against them.

## Layout

```
evals/
  <release>.json          scores for every suite that ran for the release (generated)
  golden/                 golden cases, by layer and suite (curated in the private repo)
    l1/boundary/*.json    clause-boundary F1 cases (per source family)
    l2/…                  extraction coverage / precision / dedupe cases
    l3/…  l4/…  l5/…  l6/…  l7/…
  contributed/<suite>.json  golden cases accepted through the contribution flow
                            (kind = golden_case), attributed to their contributor
  harness.py              the scorer (stdlib only)
```

## Golden case shape

```json
{
  "suite": "l7_linker",
  "description": "Which obligations does each enforcement notice cite?",
  "cases": [
    {"id": "fca-2023-001", "event": "ENF-…", "expected": ["OBL:uksi/2017/692#regulation-27", "OBL:…"]},
    {"id": "fca-2023-002", "expected_kind": "fine", "expected_materiality": "high"}
  ]
}
```

A candidate produces `predictions.json` as `{"<case id>": <predicted value>}`.
The harness scores each case by the type of `expected`:

- a set-like `expected` (list of ids or `[a, b]` pairs) → precision / recall / F1;
- a scalar `expected` → exact match;
- `expected_*` keys → per-key exact match; the case passes when every key matches.

## Running

```bash
# validate every golden file under a directory
python evals/harness.py --validate evals/golden

# score predictions against one suite (default threshold 0.90 mean case score)
python evals/harness.py --golden evals/golden/l7/linker/core.json \
                        --predictions my_links.json --threshold 0.95 --json
```

Exit status is `0` when the suite passes its threshold and `1` otherwise, so the
harness drops straight into CI.

## Contributing cases

Submit a `golden_case` contribution (see `../governance/CONTRIBUTING.md`).
Accepted cases land in `contributed/<suite>.json` on the next release with your
handle in `RELEASE_NOTES/`. Cases must be reproducible from a public source and
must not carry restricted clause text verbatim.

Licence: harness Apache-2.0; golden data ODC-By 1.0 (see `../LICENSES/`).

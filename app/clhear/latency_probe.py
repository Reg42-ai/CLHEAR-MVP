"""Time every page and API route against its budget, as a signed-in reviewer.

    python -m app.clhear.latency_probe --base-url https://clhear.reg42.ai [--rounds 5] [--output s3://.../x.json]

Runs inside the VPC with the web service's own environment: it signs a session
for the first allowlisted reviewer with the configured secret and sends the
CloudFront origin header when one is configured. No credential is printed or
written. Exit code 1 when any route misses its budget or answers 5xx.
"""
from __future__ import annotations

import argparse
import json
import statistics
import sys
import time
import urllib.error
import urllib.request
from datetime import datetime, timezone

PAGE_BUDGET_MS = 300
API_P95_BUDGET_MS = 1000
HARD_CEILING_MS = 5000
PAGES = ("/", "/signin", "/signup", "/stack", "/l1", "/sources", "/l2", "/l3", "/l4", "/l5", "/l6", "/l7", "/l8",
         "/status", "/security", "/build", "/terms")
APIS = ("/api/clhear/health", "/api/clhear/layers", "/api/clhear/sources", "/api/clhear/l1/progress",
        "/api/clhear/layers/L2", "/api/clhear/layers/L3", "/api/clhear/layers/L4", "/api/clhear/layers/L5",
        "/api/clhear/layers/L6", "/api/clhear/layers/L7", "/api/clhear/layers/L8", "/l1/sources",
        "/interop/graphql", "/status.json")
V1 = ("/v1/layers", "/v1/releases/latest")


def _headers(kind: str) -> dict:
    from app.clhear.accounts import SESSION_COOKIE, session_token
    from app.clhear.app_auth import parse_app_keys
    from app.clhear.settings import get_settings

    settings = get_settings()
    headers = {"User-Agent": "clhear-latency-probe", "Accept": "text/html" if kind == "page" else "application/json"}
    if settings.clhear_origin_verify_secret:
        headers["X-CLHEAR-Origin"] = settings.clhear_origin_verify_secret
    if kind == "v1":
        app_id, entry = next(iter(parse_app_keys(settings.clhear_app_keys).items()))
        headers.update({"X-App-Id": app_id, "Authorization": f"Bearer {entry['secrets'][0]}"})
    else:
        reviewer = sorted(settings.reviewer_set)[0]
        token = session_token({"id": "latency-probe", "email": reviewer, "display_name": "Latency probe"})
        headers["Cookie"] = f"{SESSION_COOKIE}={token}"
    return headers


def _get(url: str, headers: dict) -> tuple[int | None, float]:
    request = urllib.request.Request(url, headers=headers)
    started = time.monotonic()
    try:
        with urllib.request.urlopen(request, timeout=HARD_CEILING_MS / 1000 * 2) as response:
            response.read()
            status = response.status
    except urllib.error.HTTPError as error:
        error.read()
        status = error.code
    except Exception:  # noqa: BLE001 — a timeout is a miss, recorded as such
        status = None
    return status, (time.monotonic() - started) * 1000


def probe(base_url: str, *, rounds: int = 5) -> dict:
    routes = [(p, "page") for p in PAGES] + [(p, "api") for p in APIS] + [(p, "v1") for p in V1]
    results = []
    for path, kind in routes:
        headers = _headers(kind)
        timings, statuses = [], []
        for _ in range(rounds):
            status, ms = _get(base_url.rstrip("/") + path, headers)
            statuses.append(status)
            timings.append(ms)
        ordered = sorted(timings)
        p95 = ordered[min(len(ordered) - 1, int(round(0.95 * (len(ordered) - 1))))]
        budget = PAGE_BUDGET_MS if kind == "page" else API_P95_BUDGET_MS
        bad_status = any(s is None or s >= 500 for s in statuses)
        results.append({"path": path, "kind": kind, "statuses": sorted(set(str(s) for s in statuses)),
                        "median_ms": round(statistics.median(timings)), "p95_ms": round(p95), "max_ms": round(max(timings)),
                        "budget_ms": budget, "passed": not bad_status and p95 <= budget and max(timings) <= HARD_CEILING_MS})
    return {"base_url": base_url, "rounds": rounds, "checked_at": datetime.now(timezone.utc).isoformat(),
            "budgets": {"page_ms": PAGE_BUDGET_MS, "api_p95_ms": API_P95_BUDGET_MS, "ceiling_ms": HARD_CEILING_MS},
            "passed": all(r["passed"] for r in results), "routes": results}


def main(argv=None) -> int:
    parser = argparse.ArgumentParser(prog="python -m app.clhear.latency_probe")
    parser.add_argument("--base-url", required=True)
    parser.add_argument("--rounds", type=int, default=5)
    parser.add_argument("--output", default="")
    args = parser.parse_args(sys.argv[1:] if argv is None else argv)
    report = probe(args.base_url, rounds=max(1, args.rounds))
    body = json.dumps(report, indent=2)
    if args.output.startswith("s3://"):
        import boto3

        bucket, key = args.output[5:].split("/", 1)
        boto3.client("s3").put_object(Bucket=bucket, Key=key, Body=body.encode(), ContentType="application/json",
                                      ServerSideEncryption="AES256")
    print(body)
    return 0 if report["passed"] else 1


if __name__ == "__main__":
    raise SystemExit(main())

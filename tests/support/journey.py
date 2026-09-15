"""Offline integration journey (HLD v2 §5 acceptance): "describe your organization" → blueprint,
end to end, from a fresh browser.

Profile: a global retail broker with UK / EU / US licences. Two modes:

* ``--api`` (default): drive the public API exactly as the SDKs do — intake,
  the questions L4 still needs, the narrated build, then the evidence chain
  (node page, OSCAL export, change feed, eval gates). Prints the narrative and
  asserts the < 60 s budget and "no key, no session" (I9).
* ``--e2e``: the same journey in a fresh headless Chromium context through the
  real front door with Playwright: type the description, answer the
  questions, watch the layers narrate, follow the blueprint link. Records a
  screenshot per step under ``--artifacts``.

Starts the app itself unless ``--base-url`` points at a running instance.
A self-started app has an empty corpus (ingestion needs the network and the
inference gateway); ``--synthetic-corpus`` loads the same offline UK corpus the
L6 tests use so the layers have something to narrate.

    python -m tests.support.journey
    python -m tests.support.journey --synthetic-corpus --e2e --artifacts artifacts/clhear-test-journey
    python -m tests.support.journey --base-url http://127.0.0.1:8001 --api --e2e
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.error
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[2]
import tempfile
from urllib.parse import urlparse


def local_url(value: str) -> str:
    parsed = urlparse(value)
    if parsed.scheme != "http" or parsed.hostname not in {"127.0.0.1", "localhost", "::1"}:
        raise ValueError("Test journeys may only access a local HTTP test instance")
    return value

DESCRIPTION = ("We are a global retail equities broker holding client money, FCA authorised in the UK, "
               "with EU and US licences serving retail clients online.")
BUDGET_S = 60.0


class Journey:
    def __init__(self, base_url: str, *, quiet: bool = False):
        self.base = local_url(base_url).rstrip("/")
        self.quiet = quiet
        self.t0 = time.perf_counter()

    def say(self, msg: str) -> None:
        if not self.quiet:
            print(f"[{time.perf_counter() - self.t0:6.2f}s] {msg}")

    def call(self, method: str, path: str, body: dict | None = None) -> dict:
        req = urllib.request.Request(self.base + path, method=method, headers={"Accept": "application/json"})
        data = None
        if body is not None:
            data = json.dumps(body).encode()
            req.add_header("Content-Type", "application/json")
        try:
            with urllib.request.urlopen(req, data=data, timeout=120) as r:
                return json.loads(r.read() or b"null")
        except urllib.error.HTTPError as exc:
            raise SystemExit(f"{method} {path} -> {exc.code}: {exc.read().decode()[:400]}")

    # ------------------------------------------------------------------ api journey

    def run_api(self) -> dict:
        self.say(f"Solon: {self.base}/solon  (no account, no key)")
        home = urllib.request.urlopen(self.base + "/solon", timeout=30).read().decode()
        assert "Describe your organization" in home, "front door did not render"
        assert "paywall" in home.lower(), "front door must say the agnostic blueprint is open"

        self.say(f'Solon, read this: "{DESCRIPTION}"')
        state = self.call("POST", "/solon/intake", {"text": DESCRIPTION})
        for e in state["evidence"]:
            self.say(f"  read {e['attribute']} = {e['value']}   (cue: “{e['cue']}”)")
        asked: list[str] = []
        n = 0
        while not state.get("complete"):
            n += 1
            self.say(f"Q{n} [{state['attribute']}] {state['question']}")
            if state.get("why"):
                self.say(f"     why: {state['why'][:140]}")
            suggested = state.get("suggested") or [o["value"] for o in state["options"] if o.get("valid", True)][:1]
            value = suggested if state.get("type") == "list" else (suggested[0] if suggested else None)
            self.say(f"     answer: {value}")
            state = self.call("POST", "/solon/answer", {"attributes": state["attributes"], "attribute": state["attribute"],
                                                        "value": value, "asked": asked, "merge": bool(state.get("merge"))})
            asked = state["asked"]
            if state.get("rejected"):
                self.say(f"     rejected (closed world): {state['rejected']}")
        assert n <= 3, f"Solon asked {n} questions; the contract is at most 3"
        assert state["validation"]["valid"], f"profile invalid: {state['validation']['errors']}"
        self.say(f"Profile valid after {n} question(s): {json.dumps(state['attributes'])}")

        self.say("Composing — one step per layer:")
        build = self.call("POST", "/solon/build", {"attributes": state["attributes"], "name": "Offline integration journey · global retail broker"})
        for s in build["steps"]:
            if s["layer"] == "done":
                continue
            self.say(f"  {s['layer']:<3} {s.get('title', '')}  ({s.get('ms', 0)} ms)")
        self.say(f"Blueprint {build['blueprint_id']} for profile {build['profile_id']} in {build['total_ms']} ms "
                 f"(budget {build['budget_ms']} ms, within: {build['within_budget']})")
        assert build["ok"] and build["within_budget"], "build failed or exceeded budget"
        assert build["total_ms"] < BUDGET_S * 1000

        self.say("Evidence chain:")
        node = self.call("GET", f"/explore/node/{build['blueprint_id']}")
        self.say(f"  node page: kind={node['kind']} why={len(node['why'])} history={len(node['history'])} "
                 f"neighbours={len(node['neighbours'])} who_else={len(node['who_else'])}")
        bp = self.call("GET", f"/l6/blueprints/{build['blueprint_id']}")
        comp = bp.get("composition") or {}
        self.say(f"  blueprint: items={len(comp.get('items', []))} coverage={comp.get('coverage_summary', {})} "
                 f"minimal={bp.get('check', {}).get('minimal', bp.get('check'))}")
        oscal = self.call("GET", f"/l6/blueprints/{build['blueprint_id']}/export?format=oscal")
        self.say(f"  OSCAL SSP: {list(oscal.keys())[:3]}")
        feed = self.call("GET", "/watch/feed?limit=5")
        self.say(f"  change feed: {feed['count']} recent change(s) {feed['counts']}")
        evals = self.call("GET", "/evals/summary")
        self.say(f"  eval gates: " + ", ".join(f"{k}={'pass' if v['passed'] else 'open'}" for k, v in evals["layers"].items()))
        elapsed = time.perf_counter() - self.t0
        self.say(f"Done in {elapsed:.1f}s wall clock — no key, no session, no paywall.")
        return {"blueprint_id": build["blueprint_id"], "profile_id": build["profile_id"], "questions": n,
                "total_ms": build["total_ms"], "wall_s": round(elapsed, 2)}

    # ------------------------------------------------------------------ browser journey

    def run_e2e(self, artifacts: Path | None) -> dict:
        from playwright.sync_api import expect, sync_playwright

        shots = 0

        def shot(page, name: str) -> None:
            nonlocal shots
            if artifacts:
                artifacts.mkdir(parents=True, exist_ok=True)
                shots += 1
                page.screenshot(path=str(artifacts / f"{shots:02d}-{name}.png"), full_page=True)

        with sync_playwright() as p:
            browser = p.chromium.launch()
            ctx = browser.new_context(viewport={"width": 1280, "height": 900})  # fresh: no storage, no cookies
            page = ctx.new_page()
            t0 = time.perf_counter()
            self.say("Fresh browser → front door")
            page.goto(self.base + "/solon", wait_until="networkidle", timeout=60_000)
            expect(page.get_by_role("heading", name="Describe your organization.")).to_be_visible(timeout=30_000)
            shot(page, "front-door")

            page.get_by_label("Describe your organization").fill(DESCRIPTION)
            page.get_by_role("button", name="Ask Solon").click()
            self.say("Typed the description, asked Solon")

            answered = 0
            link = page.locator("a[href^='/l6#BLU-']").first
            while not link.count():
                page.wait_for_timeout(250)
                if page.locator(".err").count() and page.locator(".err").first.is_visible():
                    raise SystemExit("front door showed an error: " + page.locator(".err").first.inner_text())
                cont = page.get_by_role("button", name="Continue")
                if cont.count() and cont.first.is_visible() and cont.first.is_enabled():
                    answered += 1
                    shot(page, f"question-{answered}")
                    question = page.locator("#qh").inner_text()
                    if not page.locator("label.opt input:checked").count():
                        page.locator("label.opt input").first.check()  # nothing suggested: take the first valid option
                    self.say(f"Q{answered}: “{question}” → accepted the suggested options")
                    cont.first.click()
                    page.wait_for_timeout(400)
                    continue
                if time.perf_counter() - t0 > BUDGET_S + 30:
                    shot(page, "timeout")
                    raise SystemExit("journey exceeded the budget")
            assert answered <= 3, f"{answered} questions asked"

            self.say("Layers narrating…")
            expect(link).to_be_visible(timeout=int(BUDGET_S * 1000))
            elapsed = time.perf_counter() - t0
            shot(page, "blueprint")
            href = link.get_attribute("href")
            self.say(f"Blueprint link visible after {elapsed:.1f}s: {href}")
            assert elapsed < BUDGET_S, f"{elapsed:.1f}s exceeds the {BUDGET_S:.0f}s budget"

            # Evidence one click away: follow the blueprint into the layer browser.
            link.click()
            page.wait_for_load_state("networkidle")
            expect(page.locator("main")).to_be_visible()
            shot(page, "l6-browser")
            self.say("Followed the blueprint into /l6 — evidence one click away")
            ctx.close()
            browser.close()
        return {"questions": answered, "wall_s": round(elapsed, 2), "blueprint_href": href, "screenshots": shots}


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def seed_synthetic_corpus(db: Path) -> None:
    """Migrate the isolated test DB and load the offline UK corpus through L1→L5 (no network, FakeProvider)."""
    if os.environ.get("AWS_EXECUTION_ENV") or os.environ.get("CLHEAR_HTTP_MODE") == "live" or os.environ.get("CLHEAR_ARTIFACT_STORE") == "s3":
        raise ValueError("Synthetic fixtures cannot be seeded in a production worker")
    if not db.resolve().is_relative_to(Path(tempfile.gettempdir()).resolve()):
        raise ValueError("Fixture databases must be disposable temporary files")
    if db.exists():
        raise ValueError("Fixture seeding requires a new database")

    sys.path.insert(0, str(ROOT))
    from app.clhear.db import make_engine, run_migrations
    from tests.test_l6_blueprints import _corpus

    engine = make_engine(f"sqlite:///{db}")
    run_migrations(engine)
    lake = Path(tempfile.mkdtemp(prefix="clhear-test-journey-lake-"))
    _corpus(engine, lake)
    # L7: the synthetic enforcement listing → events → links → calibrated scores, so
    # /l7 renders a priority view and an enforcement explorer with real rows.
    from app.clhear.l7 import enforcement as l7_enforcement
    from app.clhear.l7 import score as l7_score
    from tests.test_l7_risk import _ingest_notices

    _ingest_notices(engine, lake)
    l7_enforcement.ingest_events(engine)
    l7_enforcement.link_events(engine)
    l7_score.calibrate(engine)
    l7_score.score_obligations(engine)
    engine.dispose()


def start_app(port: int, synthetic_corpus: bool = False) -> subprocess.Popen:
    db = Path(tempfile.mkdtemp(prefix="clhear-test-journey-")) / "corpus.db"
    if synthetic_corpus:
        seed_synthetic_corpus(db)
    env = {**os.environ, "CLHEAR_HTTP_MODE": "replay", "CLHEAR_LLM_PROVIDER": "fake", "CLHEAR_ARTIFACT_STORE": "local", "CLHEAR_SESSION_SECRET": "isolated-test-session-secret", "CLHEAR_APP_KEYS": "", "DATABASE_URL": f"sqlite:///{db}", "PYTHONPATH": str(ROOT)}
    proc = subprocess.Popen([sys.executable, "-m", "uvicorn", "app.main:app", "--port", str(port), "--log-level", "warning"],
                            cwd=ROOT, env=env)
    deadline = time.time() + 60
    while time.time() < deadline:
        try:
            urllib.request.urlopen(f"http://127.0.0.1:{port}/api/clhear/health", timeout=2)
            return proc
        except Exception:
            if proc.poll() is not None:
                raise RuntimeError("app exited during startup")
            time.sleep(0.5)
    proc.terminate()
    raise RuntimeError("app did not start")


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description="CLHEAR Offline integration journey: describe → blueprint, end to end")
    ap.add_argument("--base-url", default="")
    ap.add_argument("--api", action="store_true", help="API journey (default when nothing else is chosen)")
    ap.add_argument("--e2e", action="store_true", help="browser journey with Playwright")
    ap.add_argument("--artifacts", default="", help="directory for e2e screenshots")
    ap.add_argument("--json", action="store_true", help="print a JSON summary at the end")
    ap.add_argument("--synthetic-corpus", action="store_true",
                    help="load fixtures into a disposable local test database")
    args = ap.parse_args(argv)
    if args.base_url:
        local_url(args.base_url)
    if args.base_url and args.synthetic_corpus:
        ap.error("Synthetic fixtures require the isolated self-started test instance")
    if not args.api and not args.e2e:
        args.api = True

    proc = None
    base = args.base_url
    if not base:
        port = _free_port()
        proc = start_app(port, synthetic_corpus=args.synthetic_corpus)
        base = f"http://127.0.0.1:{port}"
    out: dict = {}
    try:
        journey = Journey(base)
        if args.api:
            out["api"] = journey.run_api()
        if args.e2e:
            try:
                import playwright  # noqa: F401
            except ImportError:
                print("e2e: SKIP — playwright not installed")
                return 2
            out["e2e"] = journey.run_e2e(Path(args.artifacts) if args.artifacts else None)
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(10)
            except Exception:
                proc.kill()

    if args.json:
        print(json.dumps(out, indent=2))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

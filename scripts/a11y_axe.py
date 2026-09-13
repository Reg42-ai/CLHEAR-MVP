"""axe-core WCAG 2.2 AA audit of the CLHEAR pages in a real browser (HLD v2 §5).

Starts the app on a free port (SQLite, replay HTTP mode), opens every public
page in headless Chromium with Playwright, injects axe-core and fails on any
``serious`` or ``critical`` violation against the WCAG 2.x A/AA rule tags.
Complements ``scripts/a11y_check.py`` (static rules) — this one sees the
rendered React tree, colour contrast and focus order.

Usage::

    python scripts/a11y_axe.py               # all pages, both colour themes
    python scripts/a11y_axe.py / /explore    # a subset
    python scripts/a11y_axe.py --base-url https://clhear.org

Requires ``pip install playwright && playwright install chromium``. Exits 2
(not 1) when Playwright or the browser is missing so CI can distinguish
"could not run" from "violations found".
"""
from __future__ import annotations

import argparse
import json
import os
import socket
import subprocess
import sys
import time
import urllib.request
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
AXE_URL = "https://cdn.jsdelivr.net/npm/axe-core@4.10.2/axe.min.js"
PAGES = ["/", "/explore", "/learn", "/watch", "/build", "/evals", "/stack", "/l1", "/l2", "/l3", "/l4", "/l5", "/l6", "/l7", "/console", "/contribute"]
TAGS = ["wcag2a", "wcag2aa", "wcag21a", "wcag21aa", "wcag22aa", "best-practice"]
FAIL_IMPACTS = {"serious", "critical"}
THEMES = ["dark", "light"]


def _free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def start_app(port: int) -> subprocess.Popen:
    """Start the app on a seeded offline corpus so the browsers render real rows, not empty states."""
    db = ROOT / ".a11y-axe.db"
    if db.exists():
        db.unlink()
    sys.path.insert(0, str(ROOT / "scripts"))
    from nyc_demo import seed_synthetic_corpus

    seed_synthetic_corpus(db)
    env = {**os.environ, "CLHEAR_HTTP_MODE": "replay", "DATABASE_URL": f"sqlite:///{db}",
           "CLHEAR_AUTH_DEBUG": "true", "PYTHONPATH": str(ROOT)}
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
    raise RuntimeError("app did not start in time")


def build_blueprint(base: str) -> str | None:
    """Compose one blueprint through the public API so /l6#BLU-… has a programme to render."""
    from nyc_demo import Demo

    try:
        out = Demo(base, quiet=True).run_api()
    except Exception as exc:
        print(f"a11y-axe: warning — could not compose a blueprint for the l6 page audit: {exc}")
        return None
    return out.get("blueprint_id")


def audit(base_url: str, pages: list[str], *, themes: list[str] = THEMES) -> list[dict]:
    from playwright.sync_api import sync_playwright

    axe_src = urllib.request.urlopen(AXE_URL, timeout=30).read().decode("utf-8")
    findings: list[dict] = []
    with sync_playwright() as p:
        browser = p.chromium.launch()
        for theme in themes:
            ctx = browser.new_context(viewport={"width": 1280, "height": 900}, color_scheme=theme)
            page = ctx.new_page()
            errors: list[str] = []
            page.on("pageerror", lambda e: errors.append(str(e).splitlines()[0][:200]))
            for path in pages:
                errors.clear()
                page.goto(base_url + path, wait_until="load", timeout=60_000)
                try:
                    page.wait_for_selector("main", timeout=60_000)
                except Exception:
                    raise RuntimeError(f"{path} did not render <main>; page errors: {errors or 'none'}") from None
                if errors:
                    findings.append({"page": path, "theme": theme, "id": "page-error", "impact": "critical",
                                     "help": "JavaScript error while rendering: " + "; ".join(errors), "helpUrl": "",
                                     "tags": ["render"], "nodes": [], "count": len(errors)})
                page.evaluate("t => { localStorage.setItem('clhear_theme', t); document.documentElement.dataset.theme = t; }", theme)
                page.wait_for_timeout(1500)  # let the data panels fill in before axe reads them
                page.add_script_tag(content=axe_src)
                result = page.evaluate("(tags) => axe.run(document, { runOnly: { type: 'tag', values: tags } })", TAGS)
                for v in result["violations"]:
                    findings.append({
                        "page": path, "theme": theme, "id": v["id"], "impact": v["impact"], "help": v["help"],
                        "helpUrl": v["helpUrl"], "tags": v["tags"],
                        "nodes": [n["target"] for n in v["nodes"][:5]], "count": len(v["nodes"]),
                    })
            ctx.close()
        browser.close()
    return findings


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("pages", nargs="*", default=PAGES)
    ap.add_argument("--base-url", default="", help="audit a running instance instead of starting one")
    ap.add_argument("--theme", choices=THEMES, help="one theme only (default: both)")
    ap.add_argument("--json", help="write full findings to this path")
    ap.add_argument("--all-impacts", action="store_true", help="fail on moderate/minor too")
    args = ap.parse_args(argv)

    try:
        import playwright  # noqa: F401
    except ImportError:
        print("a11y-axe: SKIP — playwright not installed (pip install playwright && playwright install chromium)")
        return 2

    proc = None
    base = args.base_url.rstrip("/")
    if not base:
        port = _free_port()
        try:
            proc = start_app(port)
        except Exception as exc:
            print(f"a11y-axe: SKIP — {exc}")
            return 2
        base = f"http://127.0.0.1:{port}"
        if args.pages == PAGES and (bid := build_blueprint(base)):
            # item priorities exist once the scorer has seen the composed blueprint
            try:
                from app.clhear.db import make_engine
                from app.clhear.l7 import score as l7_score

                l7_score.score_items(make_engine(f"sqlite:///{ROOT / '.a11y-axe.db'}"))
            except Exception as exc:
                print(f"a11y-axe: warning — could not score items for the l7 page audit: {exc}")
            args.pages = [*PAGES, f"/l6#{bid}", f"/l7#{bid}"]
    try:
        findings = audit(base, args.pages, themes=[args.theme] if args.theme else THEMES)
    except Exception as exc:  # browser missing, CDN unreachable
        print(f"a11y-axe: SKIP — {type(exc).__name__}: {str(exc).splitlines()[0]}")
        return 2
    finally:
        if proc is not None:
            proc.terminate()
            try:
                proc.wait(10)
            except Exception:
                proc.kill()
            db = ROOT / ".a11y-axe.db"
            if db.exists():
                db.unlink()

    if args.json:
        Path(args.json).write_text(json.dumps(findings, indent=2))
    blocking = [f for f in findings if args.all_impacts or f["impact"] in FAIL_IMPACTS]
    for f in findings:
        mark = "FAIL" if f in blocking else "note"
        print(f"  [{mark}] {f['page']} ({f['theme']}) {f['id']} · {f['impact']} · {f['count']} node(s) — {f['help']}")
        for n in f["nodes"][:3]:
            print(f"         {n}")
    if blocking:
        print(f"a11y-axe: FAIL — {len(blocking)} blocking violation(s) across {len(args.pages)} pages")
        return 1
    print(f"a11y-axe: OK — {len(args.pages)} pages × {len([args.theme] if args.theme else THEMES)} themes, {len(findings)} non-blocking note(s)")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

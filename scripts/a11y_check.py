"""Static WCAG 2.2 AA checks for the CLHEAR web shells (HLD v2 §5 design rules).

The pages are no-build React + htm, so the markup lives in the HTML files as
template literals. This checker reads those files and enforces the rules that
can be decided statically; ``scripts/a11y_axe.py`` runs axe-core in a real
browser for the rest. Both run in CI.

Checks (with the WCAG success criteria they serve):

* ``<html lang>`` present (3.1.1 Language of page)
* one ``<title>`` and a ``<meta name="viewport">`` that does not block zoom (1.4.4 Resize text)
* a skip link to ``#main`` and a ``<main id="main">`` landmark (2.4.1 Bypass blocks)
* every ``<img>`` has ``alt`` (1.1.1 Non-text content)
* every ``<input>``/``<textarea>``/``<select>`` has a label: ``aria-label``,
  ``aria-labelledby``, a wrapping ``<label>`` or a ``<label for>`` (1.3.1, 4.1.2)
* icon-only buttons carry ``aria-label`` (4.1.2 Name, role, value)
* ``<nav>`` landmarks have ``aria-label`` when a page has more than one (1.3.6)
* no ``outline: none`` without a ``:focus-visible`` replacement (2.4.7 Focus visible)
* ``prefers-reduced-motion`` honoured whenever an animation is declared (2.3.3)
* theme.css defines both colour schemes (dark default + ``[data-theme="light"]``)
* no ``tabindex`` greater than 0 (2.4.3 Focus order)
* no ``autofocus`` (3.2.1 On focus)

Exit 1 with a list of findings when any page fails. Run: ``python scripts/a11y_check.py``.
"""
from __future__ import annotations

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WEB = ROOT / "app" / "clhear" / "web"

TAG_OPEN = re.compile(r"<(?P<close>/?)(?P<name>[a-zA-Z][\w-]*)")


def _scan_tag(html: str, start: int) -> tuple[str, int]:
    """Attribute text of the tag opening at ``start`` and the index after its ``>``.

    htm templates put ``${…}`` expressions (arrow functions, comparisons) inside
    attributes, so ``>`` only closes the tag outside quotes and outside braces.
    """
    i = start
    depth = 0
    quote = ""
    while i < len(html):
        ch = html[i]
        if quote:
            if ch == quote:
                quote = ""
        elif depth:
            if ch == "{":
                depth += 1
            elif ch == "}":
                depth -= 1
        elif ch == "$" and html[i + 1: i + 2] == "{":
            depth = 1
            i += 1
        elif ch in "\"'":
            quote = ch
        elif ch == ">":
            return html[start:i], i + 1
        i += 1
    return html[start:], len(html)


def _attrs(raw: str) -> dict[str, str]:
    out: dict[str, str] = {}
    raw = re.sub(r"\$\{(?:[^{}]|\{[^{}]*\})*\}", "${x}", raw)
    for m in re.finditer(r"([\w:-]+)(?:\s*=\s*(\"[^\"]*\"|'[^']*'|\$\{x\}|[^\s\"'>]+))?", raw):
        name = m.group(1).lower()
        val = m.group(2) or ""
        out[name] = val.strip("\"'")
    return out


def _tags(html: str):
    for m in TAG_OPEN.finditer(html):
        attrs_raw, _ = _scan_tag(html, m.end())
        yield m.group("name").lower(), bool(m.group("close")), _attrs(attrs_raw), m.start()


def _tag_end(html: str, pos: int) -> int:
    m = TAG_OPEN.match(html, pos)
    return _scan_tag(html, m.end())[1] if m else html.find(">", pos) + 1


def check_page(path: Path) -> list[str]:
    html = path.read_text(encoding="utf-8")
    findings: list[str] = []
    name = path.name

    if not re.search(r"<html[^>]*\slang=", html):
        findings.append(f"{name}: <html> has no lang attribute (3.1.1)")
    head = html.split("</head>", 1)[0]
    if len(re.findall(r"<title>", head)) != 1:
        findings.append(f"{name}: exactly one <title> in <head> required (2.4.2)")
    vp = re.search(r"<meta[^>]*name=\"viewport\"[^>]*content=\"([^\"]*)\"", html)
    if not vp:
        findings.append(f"{name}: missing viewport meta (1.4.4)")
    elif "user-scalable=no" in vp.group(1) or re.search(r"maximum-scale=1(\.0)?\b", vp.group(1)):
        findings.append(f"{name}: viewport blocks zoom (1.4.4)")
    if 'class="skip"' not in html or 'href="#main"' not in html:
        findings.append(f"{name}: no skip link to #main (2.4.1)")
    if not re.search(r"<main[^>]*\bid=\"main\"", html):
        findings.append(f"{name}: no <main id=\"main\"> landmark (2.4.1)")
    if re.search(r"\bautofocus\b", html):
        findings.append(f"{name}: autofocus moves focus on load (3.2.1)")
    for m in re.finditer(r"tabindex=\"?(\d+)", html):
        if int(m.group(1)) > 0:
            findings.append(f"{name}: positive tabindex {m.group(1)} (2.4.3)")

    label_for: set[str] = set()
    labelled_ids: set[str] = set()
    for tag, closing, attrs, _ in _tags(html):
        if tag == "label" and not closing and "for" in attrs:
            label_for.add(attrs["for"])
        if "aria-labelledby" in attrs:
            labelled_ids.update(attrs["aria-labelledby"].split())

    depth_label = 0
    navs = 0
    nav_unlabelled = 0
    for tag, closing, attrs, pos in _tags(html):
        if tag == "label":
            depth_label += -1 if closing else 1
            continue
        if closing:
            continue
        if tag == "img" and "alt" not in attrs:
            findings.append(f"{name}: <img> without alt at offset {pos} (1.1.1)")
        if tag in ("input", "textarea", "select"):
            if attrs.get("type") in ("hidden", "submit", "button"):
                continue
            labelled = (
                "aria-label" in attrs or "aria-labelledby" in attrs or depth_label > 0
                or (attrs.get("id") and attrs["id"] in label_for)
            )
            if not labelled:
                findings.append(f"{name}: <{tag}> without a label at offset {pos} (1.3.1 / 4.1.2)")
        if tag == "button":
            end = _tag_end(html, pos)
            close = html.find("</button>", end)
            inner = html[end: close] if close > 0 else ""
            text = re.sub(r"\$\{[^}]*\}", "x", inner)
            text = re.sub(r"<[^>]+>", "", text).strip()
            if not text and "aria-label" not in attrs and "aria-labelledby" not in attrs:
                findings.append(f"{name}: <button> with no text or aria-label at offset {pos} (4.1.2)")
        if tag == "nav":
            navs += 1
            if "aria-label" not in attrs and "aria-labelledby" not in attrs:
                nav_unlabelled += 1
    if navs > 1 and nav_unlabelled:
        findings.append(f"{name}: {nav_unlabelled} of {navs} <nav> landmarks unlabelled (1.3.6)")

    styles = "\n".join(re.findall(r"<style[^>]*>(.*?)</style>", html, re.S))
    if re.search(r"outline\s*:\s*none", styles) and ":focus-visible" not in styles + _theme_css():
        findings.append(f"{name}: outline:none without a :focus-visible replacement (2.4.7)")
    if "@keyframes" in styles and "prefers-reduced-motion" not in styles + _theme_css():
        findings.append(f"{name}: animation without prefers-reduced-motion (2.3.3)")
    return findings


def _theme_css() -> str:
    p = WEB / "theme.css"
    return p.read_text(encoding="utf-8") if p.exists() else ""


def check_theme() -> list[str]:
    css = _theme_css()
    out = []
    if '[data-theme="light"]' not in css:
        out.append("theme.css: no light colour scheme ([data-theme=\"light\"]) — dark/light is a §5 design rule")
    if ":focus-visible" not in css:
        out.append("theme.css: no :focus-visible ring (2.4.7)")
    if "prefers-reduced-motion" not in css:
        out.append("theme.css: prefers-reduced-motion not honoured (2.3.3)")
    if ".skip" not in css:
        out.append("theme.css: skip link not styled (2.4.1)")
    return out


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    pages = sorted(WEB.glob("*.html"))
    if argv:
        pages = [WEB / a if not a.endswith(".html") or "/" not in a else Path(a) for a in argv]
    findings = check_theme()
    for page in pages:
        findings += check_page(page)
    if findings:
        print("a11y: FAIL")
        for f in findings:
            print("  -", f)
        return 1
    print(f"a11y: OK — {len(pages)} pages, theme.css")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())

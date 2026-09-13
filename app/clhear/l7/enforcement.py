"""L7 enforcement ingestors and linkers (HLD v2 §4.7).

Two deterministic passes plus an optional closed-world model step:

1. :func:`ingest_events` reads the clauses of every ``kind="enforcement"`` L1
   source (one clause per published outcome) and derives one
   ``enforcement_events`` row per notice — regulator, date, respondent, amount,
   kind and the provisions the notice cites verbatim. Idempotent on the
   clause's text hash; a changed notice is re-versioned, a vanished one
   invalidated (I2). Nothing is inferred beyond what the notice prints.
2. :func:`link_events` turns each cited provision into ``enforcement_links``
   (event → obligation). Citations are resolved against the registry: the
   instrument alias (``SYSC``, ``MLRs``, ``Securities Act``, a source's own
   short name …) picks the source, the normalised provision number picks the
   clause, and the obligation derived from that clause is the link. Exact
   clause matches are ``citation`` links; a citation to a whole provision whose
   obligations sit on its sub-paragraphs links them at lower confidence as
   ``instrument`` links. Nothing links without a printed citation.
3. With a router, notices that cite nothing the grammar recognises go to
   ``l7.link``: a closed menu of the regulator's live obligations, and the
   answer must quote the notice text that names the provision.

Linker precision is gated (:func:`app.clhear.platform.evals.l7_linker`).
"""
from __future__ import annotations

import json
import logging
import re
from datetime import date, datetime, timezone
from typing import Any

import sqlalchemy as sa
from sqlalchemy.engine import Connection, Engine

from app.clhear.derived_models import obligations
from app.clhear.l1.models import clauses, doc_nodes, source_versions, sources
from app.clhear.l7.models import EVENT_KINDS, enforcement_events, enforcement_links
from app.clhear.platform import events as l0_events
from app.clhear.platform import record
from app.clhear.platform.ids import next_id

log = logging.getLogger("clhear.l7.enforcement")

INGESTOR_VERSION = "l7-enforcement-v1"
LINKER_VERSION = "l7-linker-v1"

# --------------------------------------------------------------------------- notice grammar

_MONTHS = "january|february|march|april|may|june|july|august|september|october|november|december"
_MON3 = "jan|feb|mar|apr|may|jun|jul|aug|sep|sept|oct|nov|dec"
_DATE_PATTERNS = [
    (re.compile(r"\b(\d{4})-(\d{2})-(\d{2})\b"), "ymd"),
    (re.compile(r"\b(\d{1,2})/(\d{1,2})/(\d{4})\b"), "dmy"),
    (re.compile(rf"\b(\d{{1,2}})(?:st|nd|rd|th)?\s+({_MONTHS}|{_MON3})\.?,?\s+(\d{{4}})\b", re.I), "d-month-y"),
    (re.compile(rf"\b({_MONTHS}|{_MON3})\.?\s+(\d{{1,2}})(?:st|nd|rd|th)?,?\s+(\d{{4}})\b", re.I), "month-d-y"),
]
_AMOUNT = re.compile(
    r"(?P<cur>£|\$|US\$|USD|GBP|EUR|€)\s?(?P<num>\d{1,3}(?:,\d{3})+(?:\.\d+)?|\d+(?:\.\d+)?)\s?(?P<scale>million|m\b|bn|billion|k\b)?",
    re.I,
)
_KIND_CUES: tuple[tuple[str, str], ...] = (
    (r"prohibition order|prohibit(?:ed|s)? .{0,40}from performing|barred|\bbar\b|banned", "prohibition"),
    (r"suspend|suspension", "suspension"),
    (r"restitution|disgorgement|redress|compensat", "restitution"),
    (r"undertaking|voluntary requirement|VREQ", "undertaking"),
    (r"public censure|censure[sd]?\b|public statement", "censure"),
    (r"fine[sd]?\b|financial penalty|civil penalty|penalty of|monetary penalty|pay a penalty|ordered to pay", "fine"),
)
_FIRM_SUFFIX = re.compile(
    r"\b(ltd|limited|llc|llp|plc|inc|incorporated|corp|corporation|gmbh|s\.?a\.?|bank|capital|securities|"
    r"partners|group|holdings|markets|advisors|advisers|management|services|trust|company|co\b|fund|"
    r"asset|brokers?|exchange|payments)\b", re.I)
_INDIVIDUAL_CUES = re.compile(r"\b(mr|mrs|ms|dr|individual|former (?:director|ceo|cfo|trader|employee)|"
                              r"crd\s*#?\s*\d+|sole trader)\b", re.I)
_NOTICE_REF = re.compile(r"\b(?:Release No\.?|Rel\. No\.?|LR-|Lit\. Rel\. No\.?|No\.)\s*([A-Z0-9-]+)", re.I)

# --------------------------------------------------------------------------- citation grammar

# Static instrument aliases → source-key prefixes. The DB adds every source's own
# short name / instrument / name, so a source the fleet ingests is citable by the
# words the regulator uses for it. Ordered longest-first at match time.
STATIC_ALIASES: dict[str, tuple[str, ...]] = {
    "PRIN": ("fca/handbook",), "Principles for Businesses": ("fca/handbook",), "Principle": ("fca/handbook",),
    "SYSC": ("fca/handbook/SYSC",), "COBS": ("fca/handbook/COBS",), "CASS": ("fca/handbook/CASS",),
    "PROD": ("fca/handbook/PROD",), "SUP": ("fca/handbook/SUP",), "DISP": ("fca/handbook/DISP",),
    "MIFIDPRU": ("fca/handbook/MIFIDPRU",),
    "MLRs": ("uksi/2017/692",), "MLRs 2017": ("uksi/2017/692",), "MLR 2017": ("uksi/2017/692",),
    "Money Laundering Regulations": ("uksi/2017/692",),
    "Money Laundering, Terrorist Financing and Transfer of Funds (Information on the Payer) Regulations 2017": ("uksi/2017/692",),
    "GDPR": ("celex/32016R0679",), "UK GDPR": ("celex/32016R0679",), "Regulation (EU) 2016/679": ("celex/32016R0679",),
    "MiFID II": ("celex/32014L0065",), "Directive 2014/65/EU": ("celex/32014L0065",),
    "MiCA": ("celex/32023R1114",), "DORA": ("celex/32022R2554",), "MAR": ("celex/32014R0596",),
    "Securities Act": ("usc/15/securities-act",), "Securities Act of 1933": ("usc/15/securities-act",),
    "Exchange Act": ("usc/15/exchange-act",), "Securities Exchange Act of 1934": ("usc/15/exchange-act",),
    "Exchange Act Rule": ("cfr/17/240-bd", "cfr/17/reg-bi-sp"), "17 CFR 240": ("cfr/17/240-bd",),
    "17 CFR": ("cfr/17/240-bd", "cfr/17/reg-bi-sp"), "17 C.F.R.": ("cfr/17/240-bd", "cfr/17/reg-bi-sp"),
    "Regulation Best Interest": ("cfr/17/reg-bi-sp",), "Regulation S-P": ("cfr/17/reg-bi-sp",),
    "FINRA": ("finra/rule/", "finra/rulebook"), "FINRA Rule": ("finra/rule/", "finra/rulebook"),
    "31 CFR": ("cfr/31/chapter-x",), "Bank Secrecy Act": ("cfr/31/chapter-x",),
}
_UNIT = r"(?:rule|rules|regulation|regulations|reg\.|article|articles|art\.|section|sections|s\.|§|paragraph|para\.|principle|principles)"
_REF = r"(?P<ref>\d+[A-Za-z]?(?:[.\-]\d+[A-Za-z]?)*(?:\s*\([a-zA-Z0-9]+\))*\s?[RGED]?)"
_REF_LIST = r"(?P<refs>" + _REF.replace("(?P<ref>", "(?:") + r"(?:\s*(?:,|and|&)\s*" + _REF.replace("(?P<ref>", "(?:") + r")*)"


def _norm_tokens(ref: str) -> tuple[str, ...]:
    """'SYSC 6.1.1R' / 'regulation-28-2' / '28(2)' / 'art_6' → comparable tokens."""
    s = (ref or "").strip().lower()
    s = re.sub(r"^(?:prin|sysc|cobs|cass|prod|sup|disp|mifidpru)\s+", "", s)
    s = re.sub(r"^(?:regulation|reg|article|art|section|sec|rule|paragraph|para|principle|s|§)[\s._-]*", "", s)
    s = re.sub(r"(?<=\d)\s*[rged]$", "", s)  # FCA status letter
    tokens = tuple(t for t in re.split(r"[^0-9a-z]+", s) if t)
    return tokens


def _alias_index(conn: Connection) -> list[tuple[str, tuple[str, ...]]]:
    aliases: dict[str, set[str]] = {k: set(v) for k, v in STATIC_ALIASES.items()}
    for r in conn.execute(sa.select(sources.c.key, sources.c.short_name, sources.c.instrument, sources.c.name, sources.c.kind)):
        if r.kind == "enforcement":
            continue
        for name in (r.short_name, r.instrument, r.name):
            name = (name or "").strip()
            if len(name) >= 3 and not re.fullmatch(r"[\d\W]+", name):
                aliases.setdefault(name, set()).add(r.key)
    out = [(a, tuple(sorted(keys))) for a, keys in aliases.items() if keys]
    out.sort(key=lambda x: -len(x[0]))
    return out


def _alias_regex(aliases: list[tuple[str, tuple[str, ...]]]) -> re.Pattern:
    alt = "|".join(re.escape(a) for a, _ in aliases)
    return re.compile(
        rf"(?:(?P<alias1>{alt})\s+(?:{_UNIT}\s+)?{_REF_LIST})"
        rf"|(?:{_UNIT}\s+{_REF_LIST.replace('(?P<refs>', '(?P<refs2>')}\s+(?:of\s+)?(?:the\s+)?(?P<alias2>{alt}))",
        re.I,
    )


def extract_citations(text: str, aliases: list[tuple[str, tuple[str, ...]]]) -> list[dict]:
    """Every '<alias> <ref>' / '<unit> <ref> of the <alias>' the notice prints."""
    if not aliases or not text:
        return []
    lookup = {a.lower(): (a, keys) for a, keys in aliases}
    out: list[dict] = []
    seen: set[tuple[str, str]] = set()
    for m in _alias_regex(aliases).finditer(text):
        alias = (m.group("alias1") or m.group("alias2") or "").strip()
        refs = m.group("refs") or m.group("refs2") or ""
        canon, keys = lookup.get(alias.lower(), (alias, ()))
        for ref in re.split(r"\s*(?:,|and|&)\s*", refs):
            ref = ref.strip()
            if not ref:
                continue
            key = (canon.lower(), " ".join(_norm_tokens(ref)))
            if key in seen:
                continue
            seen.add(key)
            out.append({"text": m.group(0).strip(), "instrument": canon, "ref": ref, "source_keys": list(keys)})
    return out


# --------------------------------------------------------------------------- notice → event


def _parse_date(text: str) -> date | None:
    months = {m: i + 1 for i, m in enumerate(_MONTHS.split("|"))}
    months.update({m: i + 1 for i, m in enumerate(_MON3.split("|")) if m != "sept"})
    months["sept"] = 9
    for pat, kind in _DATE_PATTERNS:
        m = pat.search(text)
        if not m:
            continue
        try:
            if kind == "ymd":
                return date(int(m.group(1)), int(m.group(2)), int(m.group(3)))
            if kind == "dmy":
                d, mo, y = int(m.group(1)), int(m.group(2)), int(m.group(3))
                if mo > 12 and d <= 12:  # US listing printed month first
                    d, mo = mo, d
                return date(y, mo, d)
            if kind == "d-month-y":
                return date(int(m.group(3)), months[m.group(2).lower()], int(m.group(1)))
            if kind == "month-d-y":
                return date(int(m.group(3)), months[m.group(1).lower()], int(m.group(2)))
        except (ValueError, KeyError):
            continue
    return None


def _parse_amount(text: str) -> tuple[float | None, str]:
    best: tuple[float, str] | None = None
    for m in _AMOUNT.finditer(text):
        num = float(m.group("num").replace(",", ""))
        scale = (m.group("scale") or "").lower()
        if scale in ("million", "m"):
            num *= 1_000_000
        elif scale in ("bn", "billion"):
            num *= 1_000_000_000
        elif scale == "k":
            num *= 1_000
        cur = m.group("cur").upper()
        cur = {"£": "GBP", "$": "USD", "US$": "USD", "€": "EUR"}.get(cur, cur)
        if best is None or num > best[0]:
            best = (num, cur)
    return (best[0], best[1]) if best else (None, "")


def _parse_kind(text: str) -> str:
    lowered = text.lower()
    for pattern, kind in _KIND_CUES:
        if re.search(pattern, lowered, re.I):
            return kind
    return "other"


def _respondent(label: str, text: str) -> tuple[str, str]:
    name = (label or "").strip()
    m = re.match(r"^(?:SEC|Securities and Exchange Commission)\s+v\.?\s+(.+)$", name, re.I)
    if m:
        name = m.group(1).strip()
    name = re.sub(r"\s*\((?:CRD|crd)\s*#?\s*\d+\)", "", name).strip(" -–—:")
    if not name:
        m = re.search(r"(?:against|fined|fines|ordering|orders)\s+([A-Z][\w&.,' -]{2,80}?)(?:\s+(?:to pay|for|£|\$|€)|[.;])", text)
        name = m.group(1).strip() if m else ""
    if _FIRM_SUFFIX.search(name):
        rtype = "firm"
    elif _INDIVIDUAL_CUES.search(f"{name} {text}") or re.fullmatch(r"[A-Z][a-z]+(?:\s+[A-Z][a-z]+){1,3}", name or ""):
        rtype = "individual"
    else:
        rtype = "unknown"
    return name[:200], rtype


def parse_notice(label: str, text: str, *, regulator: str, url: str = "",
                 aliases: list[tuple[str, tuple[str, ...]]] | None = None) -> dict:
    """Structured event from one published outcome. Only what the notice prints."""
    body = f"{label}\n{text}"
    amount, currency = _parse_amount(body)
    kind = _parse_kind(body)
    if kind == "other" and amount:
        kind = "fine"
    respondent, rtype = _respondent(label, text)
    ref_m = _NOTICE_REF.search(body)
    summary = " ".join(text.split())[:1000]
    return {
        "regulator": regulator,
        "title": (label or "").strip()[:500],
        "respondent": respondent,
        "respondent_type": rtype,
        "decided_on": _parse_date(body),
        "amount": amount,
        "currency": currency,
        "kind": kind if kind in EVENT_KINDS else "other",
        "cited_refs": extract_citations(body, aliases or []),
        "notice_ref": (ref_m.group(1) if ref_m else "")[:100],
        "summary": summary,
        "url": url,
    }


def _enforcement_clauses(conn: Connection, source_key: str | None) -> list[dict]:
    """One row per published outcome: the provision-level clauses of the in-force
    version of every enforcement source (container sections are not notices)."""
    q = (
        sa.select(clauses.c.id, clauses.c.ref, clauses.c.text, clauses.c.text_hash, sources.c.key.label("source_key"),
                  sources.c.issuer, sources.c.jurisdiction, sources.c.canonical_url)
        .join(source_versions, source_versions.c.id == clauses.c.source_version_id)
        .join(sources, sources.c.id == source_versions.c.source_id)
        .join(doc_nodes, doc_nodes.c.id == clauses.c.doc_node_id)
        .where(sources.c.kind == "enforcement", source_versions.c.status == "in_force",
               doc_nodes.c.node_type == "provision")
        .order_by(sources.c.key, clauses.c.ordering)
    )
    if source_key:
        q = q.where(sources.c.key == source_key)
    return [dict(r) for r in conn.execute(q).mappings()]


def _live_event(event_id: str):
    return sa.and_(enforcement_events.c.id == event_id, enforcement_events.c.valid_to.is_(None))


def _why(subject: str, summary: str, *, inputs: tuple, confidence: float | None, evidence: list[dict]) -> record.WhyTrail:
    return record.WhyTrail(layer="L7", subject_ref=subject, reasoning_summary=summary, evidence_refs=evidence,
                           inputs=inputs, agent_id="fleet:l7.enforcement", skill_version=INGESTOR_VERSION,
                           confidence=confidence, input_layers=("L1",))


def ingest_events(engine: Engine, *, source_key: str | None = None) -> dict:
    """Derive enforcement events from the enforcement sources' clauses (idempotent)."""
    stats = {"clauses": 0, "inserted": 0, "re_derived": 0, "unchanged": 0, "invalidated": 0}
    with engine.begin() as conn:
        aliases = _alias_index(conn)
        rows = _enforcement_clauses(conn, source_key)
        stats["clauses"] = len(rows)
        live_q = sa.select(enforcement_events).where(enforcement_events.c.valid_to.is_(None))
        if source_key:
            live_q = live_q.where(enforcement_events.c.source_key == source_key)
        live = {(r["source_key"], r["clause_ref"]): dict(r) for r in conn.execute(live_q).mappings()}
        seen: set[tuple[str, str]] = set()
        changed = False
        for row in rows:
            key = (row["source_key"], row["ref"])
            seen.add(key)
            existing = live.get(key)
            if existing is not None and existing["text_hash"] == row["text_hash"]:
                stats["unchanged"] += 1
                continue
            label, _, text = row["text"].partition("\n") if "\n" in row["text"] else (row["ref"], "", row["text"])
            # the clause text is "<label>\n<body>" when the adapter kept a title; a bare body keeps the ref as title
            parsed = parse_notice(label if text else row["ref"], text or row["text"], regulator=row["issuer"] or "",
                                  url=row["canonical_url"] or "", aliases=aliases)
            why = _why(f"{row['source_key']}#{row['ref']}",
                       f"enforcement outcome read from {row['source_key']} {row['ref']}: {parsed['kind']}"
                       f"{' ' + parsed['currency'] + ' ' + format(parsed['amount'], ',.0f') if parsed['amount'] else ''}; "
                       f"{len(parsed['cited_refs'])} cited provision(s)",
                       inputs=(row["text_hash"], INGESTOR_VERSION), confidence=0.9 if parsed["decided_on"] else 0.7,
                       evidence=[{"layer": "L1", "clause_id": row["id"], "text_hash": row["text_hash"]}])
            trail = why.write(conn)
            if existing is not None:
                record.invalidate(conn, enforcement_events, _live_event(existing["id"]), why=trail,
                                  reason="notice text changed; re-derived")
                eid = existing["id"]
                # invalidate bumped the closed row's version; the new live row sits above it
                version = (existing["version"] or 1) + 2
                stats["re_derived"] += 1
            else:
                eid = next_id(conn, "ENF")
                version = 1
                stats["inserted"] += 1
            values = {
                "id": eid,
                **{k: v for k, v in parsed.items()},
                "jurisdiction": row["jurisdiction"] or "",
                "source_key": row["source_key"], "clause_ref": row["ref"], "clause_id": row["id"],
                "text_hash": row["text_hash"], "version": version, "derived_by": "fleet:l7.enforcement",
                "confidence": why.confidence, "inputs_hash": record.inputs_hash(row["text_hash"], INGESTOR_VERSION),
            }
            record.write(conn, enforcement_events, values, why=trail, valid_from=parsed["decided_on"] or date.today(),
                         jurisdictions=[row["jurisdiction"]] if row["jurisdiction"] else None)
            changed = True
        for key, existing in live.items():
            if key in seen:
                continue
            trail = _why(existing["id"], "notice no longer published in the in-force listing", inputs=(existing["text_hash"],),
                         confidence=None, evidence=[]).write(conn)
            record.invalidate(conn, enforcement_events, _live_event(existing["id"]), why=trail,
                              reason="notice vanished from the source")
            stats["invalidated"] += 1
            changed = True
        if changed:
            l0_events.emit(conn, layer="L7", kind="clhear.l7.enforcement_changed", subject_ref=source_key or "*",
                           payload={k: v for k, v in stats.items()}, producer="fleet:l7.enforcement")
    return stats


# --------------------------------------------------------------------------- linker


def _registry_index(conn: Connection) -> dict[str, list[tuple[tuple[str, ...], str, str]]]:
    """source_key → [(normalised clause tokens, obligation id, clause_ref)] for live obligations."""
    out: dict[str, list[tuple[tuple[str, ...], str, str]]] = {}
    for r in conn.execute(sa.select(obligations.c.id, obligations.c.source_key, obligations.c.clause_ref)
                          .where(obligations.c.status.in_(("derived", "validated")))):
        out.setdefault(r.source_key, []).append((_norm_tokens(r.clause_ref), r.id, r.clause_ref))
    return out


def index_from_rows(rows: list[dict]) -> dict[str, list[tuple[tuple[str, ...], str, str]]]:
    """Registry index from plain ``{source_key, clause_ref[, id]}`` rows (golden evals, tests)."""
    out: dict[str, list[tuple[tuple[str, ...], str, str]]] = {}
    for r in rows:
        oid = r.get("id") or f"OBL:{r['source_key']}#{r['clause_ref']}"
        out.setdefault(r["source_key"], []).append((_norm_tokens(r["clause_ref"]), oid, r["clause_ref"]))
    return out


def link_text(text: str, index: dict[str, list[tuple[tuple[str, ...], str, str]]],
              extra_aliases: dict[str, list[str]] | None = None) -> list[dict]:
    """Deterministic pass over one notice text: citations → resolved links (no DB)."""
    aliases = {k: set(v) for k, v in STATIC_ALIASES.items()}
    for name, keys in (extra_aliases or {}).items():
        aliases.setdefault(name, set()).update(keys)
    alias_list = sorted(((a, tuple(sorted(k))) for a, k in aliases.items() if k), key=lambda x: -len(x[0]))
    best: dict[str, dict] = {}
    for c in extract_citations(text, alias_list):
        for h in resolve_citation(c, index):
            prev = best.get(h["obligation_id"])
            if prev is None or h["confidence"] > prev["confidence"]:
                best[h["obligation_id"]] = {**h, "citation": c["text"]}
    return sorted(best.values(), key=lambda o: (-o["confidence"], o["obligation_id"]))


def _sources_for(prefixes: list[str], index: dict[str, Any]) -> list[str]:
    keys = []
    for p in prefixes:
        if p in index:
            keys.append(p)
        elif p.endswith("/"):
            keys.extend(k for k in index if k.startswith(p))
    return list(dict.fromkeys(keys))


def resolve_citation(citation: dict, index: dict[str, list[tuple[tuple[str, ...], str, str]]]) -> list[dict]:
    """[(obligation id, method, confidence)] a printed citation resolves to."""
    want = _norm_tokens(citation["ref"])
    if not want:
        return []
    out: list[dict] = []
    for skey in _sources_for(citation.get("source_keys") or [], index):
        for tokens, oid, clause_ref in index.get(skey, []):
            if not tokens:
                continue
            if tokens == want:
                out.append({"obligation_id": oid, "method": "citation", "confidence": 0.95, "clause_ref": clause_ref})
            elif len(want) > len(tokens) and want[: len(tokens)] == tokens:
                # the notice cites a sub-paragraph; the obligation sits on the provision
                out.append({"obligation_id": oid, "method": "citation", "confidence": 0.85, "clause_ref": clause_ref})
            elif len(tokens) > len(want) and tokens[: len(want)] == want:
                # the notice cites the provision as a whole; its obligations sit on sub-paragraphs
                out.append({"obligation_id": oid, "method": "instrument", "confidence": 0.7, "clause_ref": clause_ref})
            elif len(want) >= 2 and len(tokens) > len(want) and tokens[-len(want):] == want:
                # CFR style: the notice prints the rule number ('15l-1'), the registry the part-qualified
                # section ('§ 240.15l-1'); two or more matching tokens make the suffix unambiguous
                out.append({"obligation_id": oid, "method": "citation", "confidence": 0.85, "clause_ref": clause_ref})
    exact = [o for o in out if o["confidence"] >= 0.95]
    if exact:
        out = exact  # the cited provision is in the registry; its parent / children are not what the notice names
    best: dict[str, dict] = {}
    for o in out:
        if o["obligation_id"] not in best or o["confidence"] > best[o["obligation_id"]]["confidence"]:
            best[o["obligation_id"]] = o
    return sorted(best.values(), key=lambda o: (-o["confidence"], o["obligation_id"]))


def _llm_links(llm, event: dict, candidates: list[dict]) -> list[dict]:
    """Closed-world fallback: pick from the regulator's live obligations, quote the notice."""
    from app.clhear.platform.gateway import parse_json_object
    from app.clhear.platform.router import complete

    if llm is None or not candidates:
        return []
    menu = "\n".join(f"- {c['id']} :: {c['source_key']} {c['clause_ref']} — {c['title'][:120]}" for c in candidates[:60])
    prompt = (
        "An enforcement notice is below. Which of the listed obligations does the notice say were breached? "
        "Answer only from the menu, and for each pick copy the exact words of the notice that name the provision. "
        "If the notice names no listed provision, answer with an empty list.\n\n"
        f"NOTICE:\n{event['title']}\n{event['summary']}\n\nMENU:\n{menu}\n\n"
        'JSON: {"links": [{"obligation_id": "...", "quote": "..."}]}'
    )
    try:
        result = complete(llm, "l7.link", prompt=prompt, system="Closed-world enforcement linker. JSON only.",
                          required_keys=["links"], max_tokens=600)
        parsed = parse_json_object(result.text)
    except Exception as exc:  # the deterministic pass stands on its own
        log.warning("l7.link unavailable for %s: %s", event["id"], exc)
        return []
    allowed = {c["id"] for c in candidates}
    haystack = " ".join(f"{event['title']} {event['summary']}".split()).lower()
    out = []
    for link in parsed.get("links") or []:
        oid, quote = str(link.get("obligation_id") or ""), " ".join(str(link.get("quote") or "").split())
        if oid in allowed and len(quote) >= 6 and quote.lower() in haystack:
            out.append({"obligation_id": oid, "method": "llm", "confidence": 0.75, "citation": quote})
    return out


def link_events(engine: Engine, llm=None, *, source_key: str | None = None) -> dict:
    """Resolve every live event's citations into enforcement_links (idempotent per pair)."""
    stats = {"events": 0, "linked_events": 0, "links": 0, "new_links": 0, "unresolved_citations": 0, "llm_links": 0}
    with engine.begin() as conn:
        index = _registry_index(conn)
        q = sa.select(enforcement_events).where(enforcement_events.c.valid_to.is_(None))
        if source_key:
            q = q.where(enforcement_events.c.source_key == source_key)
        events = [dict(r) for r in conn.execute(q).mappings()]
        existing: dict[str, dict[str, dict]] = {}
        for r in conn.execute(sa.select(enforcement_links).where(enforcement_links.c.valid_to.is_(None))).mappings():
            existing.setdefault(r["event_id"], {})[r["obligation_id"]] = dict(r)
        candidates_by_regulator: dict[str, list[dict]] = {}
        if llm is not None:
            for r in conn.execute(sa.select(obligations.c.id, obligations.c.source_key, obligations.c.clause_ref,
                                            obligations.c.title, obligations.c.jurisdiction)
                                  .where(obligations.c.status.in_(("derived", "validated")))).mappings():
                candidates_by_regulator.setdefault(r["jurisdiction"], []).append(dict(r))
        # links of events that were invalidated since the last pass close with them (I2)
        live_ids = {ev["id"] for ev in events}
        for event_id, by_oid in existing.items():
            if event_id in live_ids:
                continue
            still_live = conn.execute(sa.select(sa.func.count()).select_from(enforcement_events)
                                      .where(_live_event(event_id))).scalar_one()
            if still_live:
                continue  # another source's event, outside this pass's source_key filter
            trail = record.WhyTrail(layer="L7", subject_ref=event_id, reasoning_summary="event invalidated; links close with it",
                                    evidence_refs=[], inputs=(event_id, LINKER_VERSION), agent_id="fleet:l7.linker",
                                    skill_version=LINKER_VERSION, confidence=None).write(conn)
            for link in by_oid.values():
                record.invalidate(conn, enforcement_links, enforcement_links.c.id == link["id"], why=trail,
                                  reason="event no longer live")
                stats["closed_links"] = stats.get("closed_links", 0) + 1
        for ev in events:
            stats["events"] += 1
            cited = ev["cited_refs"] if isinstance(ev["cited_refs"], list) else json.loads(ev["cited_refs"] or "[]")
            wanted: dict[str, dict] = {}
            for c in cited:
                hits = resolve_citation(c, index)
                if not hits:
                    stats["unresolved_citations"] += 1
                for h in hits:
                    prev = wanted.get(h["obligation_id"])
                    if prev is None or h["confidence"] > prev["confidence"]:
                        wanted[h["obligation_id"]] = {**h, "citation": c["text"]}
            if not wanted and llm is not None:
                for h in _llm_links(llm, ev, candidates_by_regulator.get(ev["jurisdiction"], [])):
                    wanted[h["obligation_id"]] = h
                    stats["llm_links"] += 1
            if wanted:
                stats["linked_events"] += 1
            have = existing.get(ev["id"], {})
            for oid, h in wanted.items():
                stats["links"] += 1
                if oid in have and have[oid]["event_text_hash"] == ev["text_hash"]:
                    continue
                why = record.WhyTrail(
                    layer="L7", subject_ref=ev["id"], input_layers=("L1", "L2"),
                    reasoning_summary=f"{ev['id']} cites {h['citation'][:120]!r} → {oid} ({h['method']})",
                    evidence_refs=[{"layer": "L7", "event_id": ev["id"], "text_hash": ev["text_hash"]},
                                   {"layer": "L2", "obligation_id": oid}],
                    inputs=(ev["text_hash"], oid, LINKER_VERSION), agent_id="fleet:l7.linker",
                    skill_version=LINKER_VERSION, confidence=h["confidence"])
                trail = why.write(conn)
                if oid in have:
                    record.invalidate(conn, enforcement_links, enforcement_links.c.id == have[oid]["id"], why=trail,
                                      reason="event re-derived; link re-issued")
                record.write(conn, enforcement_links, {"event_id": ev["id"], "obligation_id": oid, "citation": h["citation"][:500],
                                                       "method": h["method"], "event_text_hash": ev["text_hash"],
                                                       "confidence": h["confidence"], "derived_by": "fleet:l7.linker"},
                             why=trail, valid_from=ev["decided_on"] or date.today())
                stats["new_links"] += 1
            # links whose obligation the notice no longer supports (re-derived notice) close
            for oid, link in have.items():
                if oid not in wanted and link["event_text_hash"] != ev["text_hash"]:
                    trail = record.WhyTrail(layer="L7", subject_ref=ev["id"], reasoning_summary="notice re-derived; citation gone",
                                            evidence_refs=[], inputs=(ev["text_hash"],), agent_id="fleet:l7.linker",
                                            skill_version=LINKER_VERSION, confidence=None).write(conn)
                    record.invalidate(conn, enforcement_links, enforcement_links.c.id == link["id"], why=trail,
                                      reason="citation no longer in the notice")
    return stats


# --------------------------------------------------------------------------- reads


def _plain(row: dict) -> dict:
    out = dict(row)
    for k, v in out.items():
        if isinstance(v, (date, datetime)):
            out[k] = v.isoformat()
    if out.get("cited_refs") is not None and isinstance(out["cited_refs"], str):
        out["cited_refs"] = json.loads(out["cited_refs"])
    if out.get("amount") is not None:
        out["amount"] = float(out["amount"])
    if out.get("confidence") is not None:
        out["confidence"] = float(out["confidence"])
    return out


def list_events(conn: Connection, *, regulator: str | None = None, jurisdiction: str | None = None, kind: str | None = None,
                since: date | None = None, q: str | None = None, obligation_id: str | None = None, limit: int = 200,
                include_invalidated: bool = False) -> list[dict]:
    query = sa.select(enforcement_events)
    if not include_invalidated:
        query = query.where(enforcement_events.c.valid_to.is_(None))
    if regulator:
        query = query.where(sa.func.lower(enforcement_events.c.regulator).like(f"%{regulator.lower()}%"))
    if jurisdiction:
        query = query.where(enforcement_events.c.jurisdiction == jurisdiction.upper())
    if kind:
        query = query.where(enforcement_events.c.kind == kind)
    if since:
        query = query.where(enforcement_events.c.decided_on >= since)
    if q:
        like = f"%{q.lower()}%"
        query = query.where(sa.or_(sa.func.lower(enforcement_events.c.title).like(like),
                                   sa.func.lower(enforcement_events.c.respondent).like(like),
                                   sa.func.lower(enforcement_events.c.summary).like(like)))
    if obligation_id:
        ids = sa.select(enforcement_links.c.event_id).where(enforcement_links.c.obligation_id == obligation_id,
                                                            enforcement_links.c.valid_to.is_(None))
        query = query.where(enforcement_events.c.id.in_(ids))
    query = query.order_by(enforcement_events.c.decided_on.desc().nullslast(), enforcement_events.c.id.desc()).limit(limit)
    return [_plain(r) for r in conn.execute(query).mappings()]


def links_for_events(conn: Connection, event_ids: list[str]) -> dict[str, list[dict]]:
    if not event_ids:
        return {}
    out: dict[str, list[dict]] = {}
    for r in conn.execute(sa.select(enforcement_links).where(enforcement_links.c.event_id.in_(event_ids),
                                                             enforcement_links.c.valid_to.is_(None))).mappings():
        out.setdefault(r["event_id"], []).append(_plain(r))
    return out


def events_for_obligation(conn: Connection, obligation_id: str) -> list[dict]:
    """'Who was fined for this obligation, when, how much' — live events linked to it."""
    events = list_events(conn, obligation_id=obligation_id, limit=500)
    links = links_for_events(conn, [e["id"] for e in events])
    for e in events:
        mine = [l for l in links.get(e["id"], []) if l["obligation_id"] == obligation_id]
        e["link"] = mine[0] if mine else None
    return events


__all__ = ["INGESTOR_VERSION", "LINKER_VERSION", "STATIC_ALIASES", "events_for_obligation", "extract_citations", "index_from_rows", "link_text",
           "ingest_events", "link_events", "links_for_events", "list_events", "parse_notice", "resolve_citation"]

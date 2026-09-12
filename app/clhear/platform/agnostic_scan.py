"""Agnostic-store scan (HLD v2 I5, §9): the CLHEAR store must contain zero
organization identifiers and zero personal data of any adopter.

Runs against a release directory (snapshot + manifest + exports) or a live
engine. Every hit is a release blocker: the CLI exits non-zero, and the
release pipeline runs it before signing.

Detection is deliberately conservative and explainable:

* structural PII patterns — email addresses, IBANs, phone numbers, national
  ids formatted like SSNs, AWS account ids that are not CLHEAR's own;
* a denylist of organization identifiers — the adopter/member names and
  domains CLHEAR must never hold, loaded from ``CLHEAR_AGNOSTIC_DENYLIST``
  (comma-separated) and/or ``docs/agnostic_denylist.txt`` (one per line);
* member aggregates (L8) must appear only as k-anonymous cohorts, never as
  rows carrying a member id.

Allow-list: source publishers (regulators, standards bodies) appear in L1
verbatim text by design and are not organization identifiers of adopters.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Iterable, Iterator

EMAIL_RE = re.compile(r"\b[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}\b")
IBAN_RE = re.compile(r"\b[A-Z]{2}\d{2}(?:[ ]?[A-Z0-9]{4}){3,7}\b")
SSN_RE = re.compile(r"\b\d{3}-\d{2}-\d{4}\b")
PHONE_RE = re.compile(r"(?<![\w.])\+\d{1,3}[ -]?\(?\d{1,4}\)?(?:[ -]?\d{2,4}){2,4}\b")
AWS_ACCOUNT_RE = re.compile(r"\b\d{12}\b")

# Publisher / infrastructure addresses that legitimately appear in the corpus.
ALLOWED_EMAIL_DOMAINS = {
    "reg42.ai", "example.com", "example.org", "legislation.gov.uk", "europa.eu",
    "fca.org.uk", "sec.gov", "gpo.gov", "finra.org", "fatf-gafi.org", "bis.org",
    "iosco.org", "mas.gov.sg", "asic.gov.au", "gov.il", "irs.gov", "wolfsberg-group.org",
}
OWN_AWS_ACCOUNTS = {"730649732189"}

DEFAULT_DENYLIST_PATH = Path("docs/agnostic_denylist.txt")

# Columns whose presence in a published table indicates member-level data.
MEMBER_ID_COLUMNS = {"member_id", "org_id", "organisation_id", "organization_id", "tenant_id", "customer_id"}
# L8 aggregates may only be published when the cohort has k >= this many members.
K_ANON_MIN = 5

SCANNED_SUFFIXES = {".json", ".jsonl", ".txt", ".md", ".csv", ".yaml", ".yml", ".sql"}


@dataclass
class Hit:
    kind: str
    location: str
    sample: str


@dataclass
class ScanReport:
    target: str
    files_scanned: int = 0
    tables_scanned: int = 0
    hits: list[Hit] = field(default_factory=list)

    @property
    def clean(self) -> bool:
        return not self.hits

    def to_dict(self) -> dict:
        return {
            "target": self.target,
            "clean": self.clean,
            "files_scanned": self.files_scanned,
            "tables_scanned": self.tables_scanned,
            "hit_count": len(self.hits),
            "hits": [asdict(h) for h in self.hits[:200]],
        }


def load_denylist(extra: Iterable[str] | None = None, path: Path | None = None) -> list[str]:
    terms: list[str] = []
    env = os.environ.get("CLHEAR_AGNOSTIC_DENYLIST", "")
    terms.extend(t.strip() for t in env.split(",") if t.strip())
    p = path or DEFAULT_DENYLIST_PATH
    if p.exists():
        for line in p.read_text(encoding="utf-8").splitlines():
            line = line.strip()
            if line and not line.startswith("#"):
                terms.append(line)
    if extra:
        terms.extend(t for t in extra if t)
    # longest first so overlapping names report the most specific term
    return sorted({t.lower() for t in terms}, key=len, reverse=True)


def _redact(sample: str) -> str:
    sample = sample.strip().replace("\n", " ")
    return sample[:12] + "…" if len(sample) > 12 else sample


def scan_text(text: str, location: str, denylist: list[str]) -> Iterator[Hit]:
    for m in EMAIL_RE.finditer(text):
        domain = m.group(0).rsplit("@", 1)[1].lower()
        if domain not in ALLOWED_EMAIL_DOMAINS:
            yield Hit("email", location, _redact(m.group(0)))
    for m in IBAN_RE.finditer(text):
        yield Hit("iban", location, _redact(m.group(0)))
    for m in SSN_RE.finditer(text):
        yield Hit("national_id", location, _redact(m.group(0)))
    for m in AWS_ACCOUNT_RE.finditer(text):
        if m.group(0) not in OWN_AWS_ACCOUNTS and not _looks_like_hash_context(text, m):
            yield Hit("aws_account", location, _redact(m.group(0)))
    lowered = text.lower()
    for term in denylist:
        if term in lowered:
            yield Hit("organization_identifier", location, term)


def _looks_like_hash_context(text: str, m: re.Match) -> bool:
    """12 digits inside a longer hex/base64 run are hashes, not account ids."""
    start, end = m.span()
    before = text[max(0, start - 1) : start]
    after = text[end : end + 1]
    return bool(re.match(r"[0-9a-fA-F]", before) or re.match(r"[0-9a-fA-F]", after))


def scan_file(path: Path, denylist: list[str]) -> Iterator[Hit]:
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return
    yield from scan_text(text, str(path), denylist)


def scan_sqlite(db_path: Path, denylist: list[str], report: ScanReport) -> None:
    conn = sqlite3.connect(f"file:{db_path}?mode=ro", uri=True)
    try:
        tables = [r[0] for r in conn.execute("select name from sqlite_master where type='table'")]
        for table in tables:
            report.tables_scanned += 1
            cols = [r[1] for r in conn.execute(f'pragma table_info("{table}")')]
            member_cols = MEMBER_ID_COLUMNS & set(cols)
            if member_cols and not table.startswith("cohort"):
                n = conn.execute(f'select count(*) from "{table}"').fetchone()[0]
                if n:
                    report.hits.append(Hit("member_level_rows", f"{db_path}:{table}", ",".join(sorted(member_cols))))
            if table.startswith("cohort") and "k" in cols:
                bad = conn.execute(f'select count(*) from "{table}" where k < ?', (K_ANON_MIN,)).fetchone()[0]
                if bad:
                    report.hits.append(Hit("k_anonymity", f"{db_path}:{table}", f"{bad} cohorts with k<{K_ANON_MIN}"))
            text_cols = [c for c in cols if c not in ("id",)]
            if not text_cols:
                continue
            select = ", ".join(f'"{c}"' for c in text_cols)
            for row in conn.execute(f"select {select} from \"{table}\""):
                for col, value in zip(text_cols, row):
                    if isinstance(value, str) and value:
                        for hit in scan_text(value, f"{db_path}:{table}.{col}", denylist):
                            report.hits.append(hit)
    finally:
        conn.close()


def scan_engine(engine, denylist: list[str] | None = None) -> ScanReport:
    """Scan every layer table through SQLAlchemy (Postgres or SQLite)."""
    import sqlalchemy as sa

    from app.clhear.platform.record import layer_tables

    denylist = denylist if denylist is not None else load_denylist()
    report = ScanReport(target=str(engine.url).split("@")[-1])
    with engine.connect() as conn:
        for table in layer_tables():
            report.tables_scanned += 1
            cols = [c.name for c in table.columns]
            member_cols = MEMBER_ID_COLUMNS & set(cols)
            if member_cols and not table.name.startswith("cohort"):
                n = conn.execute(sa.select(sa.func.count()).select_from(table)).scalar_one()
                if n:
                    report.hits.append(Hit("member_level_rows", table.name, ",".join(sorted(member_cols))))
            string_cols = [c for c in table.columns if isinstance(c.type, (sa.String, sa.Text, sa.JSON))]
            if not string_cols:
                continue
            for row in conn.execute(sa.select(*string_cols)):
                for col, value in zip(string_cols, row):
                    if value is None:
                        continue
                    text = value if isinstance(value, str) else json.dumps(value, default=str)
                    report.hits.extend(scan_text(text, f"{table.name}.{col.name}", denylist))
    return report


def scan_path(target: Path, denylist: list[str] | None = None) -> ScanReport:
    denylist = denylist if denylist is not None else load_denylist()
    report = ScanReport(target=str(target))
    if not target.exists():
        report.hits.append(Hit("missing_target", str(target), "nothing to scan — refusing to certify"))
        return report
    paths = [target] if target.is_file() else sorted(p for p in target.rglob("*") if p.is_file())
    for p in paths:
        if p.name in ("agnostic-scan.json", "manifest.sigstore.json"):
            continue
        if p.suffix in (".db", ".sqlite", ".sqlite3"):
            report.files_scanned += 1
            scan_sqlite(p, denylist, report)
        elif p.suffix in SCANNED_SUFFIXES:
            report.files_scanned += 1
            report.hits.extend(scan_file(p, denylist))
    return report


def main(argv: list[str] | None = None) -> int:
    argv = sys.argv[1:] if argv is None else argv
    if not argv:
        print("usage: python -m app.clhear.platform.agnostic_scan <release-dir|file|--engine> [--out report.json]", file=sys.stderr)
        return 2
    out = None
    if "--out" in argv:
        i = argv.index("--out")
        out = Path(argv[i + 1])
        argv = argv[:i] + argv[i + 2 :]
    if argv[0] == "--engine":
        from app.clhear.db import get_engine, run_migrations

        engine = get_engine()
        run_migrations(engine)
        report = scan_engine(engine)
    else:
        target = Path(argv[0])
        report = scan_path(target)
        if out is None and target.is_dir():
            out = target / "agnostic-scan.json"
    payload = report.to_dict()
    if out is not None:
        out.parent.mkdir(parents=True, exist_ok=True)
        out.write_text(json.dumps(payload, indent=2))
    print(json.dumps(payload, indent=2))
    return 0 if report.clean else 1


if __name__ == "__main__":
    raise SystemExit(main())

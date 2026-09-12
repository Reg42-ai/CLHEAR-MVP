"""SEC + FINRA via the EDGAR channel (HLD v2 §4.1).

Two channels behind one adapter key so the fleet schedule, the polite client
(SEC requires a declared ``User-Agent`` with a contact address) and the
rights recorder stay in one place:

``channel="sec"``   SEC final rules / 17 CFR text on sec.gov — public domain
                    (17 U.S.C. § 105); refs are ``§ 240.15c3-3`` or ``Rule 15c3-3``.
``channel="finra"`` FINRA rules as published (finra.org rulebook; rule
                    changes arrive as SR-FINRA 19b-4 filings on EDGAR) —
                    *derived-only* rights: CLHEAR keeps hashes, spans and
                    derived obligations but never republishes the text.
                    Refs are ``3110`` / ``3110(a)`` / ``3110(a)(1)``.
"""
import re

from app.clhear.l1.adapters.publisher import NumberedHtmlAdapter

SEC_HEADERS = {"User-Agent": "CLHEAR by Reg42 (compliance@reg42.ai)"}

_SEC = re.compile(r"^(?P<ref>§\s*2\d{2}\.\d+[A-Za-z0-9\-]*|Rule\s+\d+[a-zA-Z]?(?:-\d+)?(?:\([a-z0-9]+\))*)(?=[\s.:—-]|$)")
_FINRA_RULE = re.compile(r"^(?P<ref>\d{4}[A-Z]?)\.\s+(?=[A-Z])")
_FINRA_SUB = re.compile(r"^(?P<ref>\([a-z]\)(?:\(\d+\))?(?:\([A-Z]\))?)\s+")
_FINRA_NUM = re.compile(r"^(?P<ref>\(\d+\)(?:\([A-Z]\))?)\s+")


class SecEdgarAdapter(NumberedHtmlAdapter):
    key = "sec_edgar"
    jurisdiction = "US"
    kind = "regulation"
    headers = SEC_HEADERS

    def __init__(self, *, channel: str = "sec", **kwargs):
        if channel not in {"sec", "finra"}:
            raise ValueError("channel must be 'sec' or 'finra'")
        self.channel = channel
        if channel == "sec":
            self.publisher = "U.S. Securities and Exchange Commission"
            self.issuer = self.publisher
            self.PROVISION = _SEC
            kwargs.setdefault("family_key", "us-broker-dealer")
            kwargs.setdefault("family_name", "US broker-dealer & listed company")
        else:
            self.publisher = "FINRA (via SEC EDGAR 19b-4 channel)"
            self.issuer = "FINRA"
            self.PROVISION = _FINRA_RULE
            kwargs.setdefault("family_key", "us-broker-dealer")
            kwargs.setdefault("family_name", "US broker-dealer & listed company")
        super().__init__(**kwargs)
        self._current_rule = ""
        self._current_sub = ""

    def meta(self):
        meta = super().meta()
        if self._meta is not None:
            return meta
        from dataclasses import replace

        basis = "public_domain" if self.channel == "sec" else "derived_only"
        return replace(meta, rights_basis=basis, publisher=self.publisher)

    # FINRA pages: "3110. Supervision" opens a rule; "(a) …" opens a subsection
    # of the current rule. Both are provisions (obligations anchor at either).
    def _parse_into(self, content, seen, *, part):
        if self.channel == "finra":
            self._current_rule = ""
            self._current_sub = ""
            self.PROVISION = _FinraMatcher()
        try:
            return super()._parse_into(content, seen, part=part)
        finally:
            if self.channel == "finra":
                self.PROVISION = _FINRA_RULE

    def make_ref(self, match, context) -> str:
        ref = " ".join(match.group("ref").split())
        if self.channel == "finra":
            if re.match(r"^\(\d", ref):  # "(1)" nests under the current lettered paragraph
                return f"{self._current_sub}{ref}" if self._current_sub else ref
            if ref.startswith("("):
                self._current_sub = f"{self._current_rule}{ref}" if self._current_rule else ref
                return self._current_sub
            self._current_rule = ref
            self._current_sub = ""
        return ref


class _FinraMatcher:
    """Try the rule pattern then the subsection pattern (stateless helper)."""

    def match(self, text: str):
        return _FINRA_RULE.match(text) or _FINRA_SUB.match(text) or _FINRA_NUM.match(text)

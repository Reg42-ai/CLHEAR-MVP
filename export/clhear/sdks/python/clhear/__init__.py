"""clhear — Python SDK for the CLHEAR public API (HLD v2 §5 "Build on it").

Open endpoints (no key): describe → blueprint, any node's why-trail, the
change feed, the published eval gates. Keyed endpoints (``X-App-Id`` +
bearer secret issued at /build): release-pinned layer reads.

    >>> from clhear import Client
    >>> c = Client("https://clhear.org")
    >>> bp = c.build("A UK retail equities broker holding client money")
    >>> bp["blueprint_id"]
    'BLU-000012'

Published from export/clhear by item 13; standard library only.
"""
from __future__ import annotations

import json
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Iterator

__version__ = "0.1.0"
__all__ = ["Client", "ClhearError"]


class ClhearError(RuntimeError):
    def __init__(self, status: int, detail: Any):
        super().__init__(f"CLHEAR API {status}: {detail}")
        self.status = status
        self.detail = detail


class Client:
    def __init__(self, base_url: str = "https://clhear.org", *, app_id: str | None = None, secret: str | None = None,
                 id_token: str | None = None, timeout: float = 60.0):
        self.base_url = base_url.rstrip("/")
        self.app_id = app_id
        self.secret = secret
        # A Cognito id token identifies the *person* for the contribution flow
        # (CLA, filing, reviews); the app key identifies the *application*.
        self.id_token = id_token
        self.timeout = timeout

    # ------------------------------------------------------------------ http

    def _request(self, method: str, path: str, *, params: dict | None = None, body: dict | None = None) -> Any:
        url = self.base_url + path
        if params:
            url += "?" + urllib.parse.urlencode({k: v for k, v in params.items() if v is not None})
        headers = {"Accept": "application/json", "User-Agent": f"clhear-python/{__version__}"}
        if self.app_id and self.secret:
            headers["X-App-Id"] = self.app_id
            headers["Authorization"] = f"Bearer {self.secret}"
        if self.id_token and (path.startswith(("/cla", "/contributions", "/contributors/me", "/roles"))):
            headers["Authorization"] = f"Bearer {self.id_token}"
        data = None
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            headers["Content-Type"] = "application/json"
        req = urllib.request.Request(url, data=data, method=method, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=self.timeout) as resp:
                raw = resp.read()
                return json.loads(raw) if raw else None
        except urllib.error.HTTPError as exc:
            try:
                detail = json.loads(exc.read()).get("detail")
            except Exception:
                detail = exc.reason
            raise ClhearError(exc.code, detail) from None

    # ------------------------------------------------------------------ open (no key)

    def intake(self, text: str) -> dict:
        """What Solon read from ``text`` and the first question it still needs."""
        return self._request("POST", "/solon/intake", body={"text": text})

    def answer(self, state: dict, value: Any) -> dict:
        """Answer the current question; returns the next question or ``complete``."""
        return self._request("POST", "/solon/answer", body={
            "attributes": state["attributes"], "attribute": state["attribute"], "value": value,
            "asked": state.get("asked", []), "merge": bool(state.get("merge")),
        })

    def build(self, text_or_attributes: str | dict, *, name: str = "", accept_suggestions: bool = True) -> dict:
        """Describe → blueprint. With ``accept_suggestions`` the SDK answers each
        question with the options Solon marks as suggested (the licences that
        permit the described products); pass a dict of attributes to skip the questions."""
        if isinstance(text_or_attributes, dict):
            return self._request("POST", "/solon/build", body={"attributes": text_or_attributes, "name": name})
        state = self.intake(text_or_attributes)
        state.setdefault("asked", [])
        while not state.get("complete"):
            if not accept_suggestions:
                raise ClhearError(422, {"question": state["question"], "attribute": state["attribute"], "options": state["options"]})
            suggested = state.get("suggested") or [o["value"] for o in state.get("options", []) if o.get("valid", True)][:1]
            value = suggested if state.get("type") == "list" else (suggested[0] if suggested else None)
            state = self.answer(state, value)
        return self._request("POST", "/solon/build", body={"attributes": state["attributes"], "name": name})

    def blueprint(self, blueprint_id: str) -> dict:
        return self._request("GET", f"/l6/blueprints/{blueprint_id}")

    def oscal_ssp(self, blueprint_id: str) -> dict:
        return self._request("GET", f"/l6/blueprints/{blueprint_id}/export", params={"format": "oscal"})

    def oscal_components(self, release: str = "") -> dict:
        return self._request("GET", "/l6/export/oscal/components", params={"release": release or None})

    def node(self, node_id: str) -> dict:
        """Any node (OBL-, BLK-, ACT-, PRF-, BLU-, LIC:…): why, history, who else, neighbours."""
        return self._request("GET", f"/explore/node/{urllib.parse.quote(node_id, safe=':/')}")

    def search(self, q: str) -> dict:
        return self._request("GET", "/explore/search", params={"q": q})

    def compare(self, jurisdictions: list[str], theme: str | None = None) -> dict:
        return self._request("GET", "/explore/compare", params={"jurisdictions": ",".join(jurisdictions), "theme": theme})

    def feed(self, *, since: str | None = None, layers: str = "L1,L2,L6", limit: int = 100) -> dict:
        return self._request("GET", "/watch/feed", params={"since": since, "layers": layers, "limit": limit})

    def evals(self, release: str | None = None) -> dict:
        return self._request("GET", "/evals/summary", params={"release": release})

    # ------------------------------------------------------------------ keyed

    def layers(self) -> dict:
        return self._request("GET", "/v1/layers")

    def releases(self) -> Any:
        return self._request("GET", "/v1/releases")

    def snapshot(self, release_id: str, layer: str) -> dict:
        return self._request("GET", f"/v1/releases/{release_id}/{layer}/snapshot")

    # ------------------------------------------------------------------ contribute (HLD v2 §6)

    def cla(self) -> dict:
        """The current CLA text, its hash, and whether the identified user signed it."""
        return self._request("GET", "/cla")

    def sign_cla(self, *, display_name: str = "") -> dict:
        return self._request("POST", "/cla/sign", body={"acknowledge_patent_grant": True, "display_name": display_name})

    def contribute(self, kind: str, proposed: dict, *, target_ref: str = "", field: str = "", layer: str | None = None,
                   evidence: list[dict] | None = None, rationale: str = "") -> dict:
        """File a contribution (correction, missing_source, equivalence, characteristic, ontology_entry, fill,
        translation, golden_case, evidence_template, enforcement_link). Needs an id_token and a signed CLA.
        Nothing is written to the registry: checks → fleet re-derivation → two reviewers → release."""
        return self._request("POST", "/contributions", body={
            "kind": kind, "proposed": proposed, "target_ref": target_ref, "field": field, "layer": layer,
            "evidence": evidence or [], "rationale": rationale, "channel": "api"})

    def contribution(self, contribution_id: str) -> dict:
        return self._request("GET", f"/contributions/{contribution_id}")

    def contributions(self, *, status: str | None = None, kind: str | None = None, contributor: str | None = None,
                      limit: int = 100) -> dict:
        return self._request("GET", "/contributions", params={"status": status, "kind": kind, "contributor": contributor, "limit": limit})

    def review(self, contribution_id: str, decision: str, note: str = "") -> dict:
        """One reviewer's decision: accept | reject | request_changes (reviewer roles only)."""
        return self._request("POST", f"/contributions/{contribution_id}/reviews", body={"decision": decision, "note": note})

    def contributors(self, limit: int = 50) -> dict:
        return self._request("GET", "/contributors", params={"limit": limit})

    def my_contributions(self) -> dict:
        return self._request("GET", "/contributors/me")

    def notifications(self, *, unread: bool = False) -> dict:
        return self._request("GET", "/contributions/notifications", params={"unread": "true" if unread else None})

    def governance(self) -> dict:
        return self._request("GET", "/governance")

    # ---- L8 fills and member benchmarks (HLD v2 §4.8; open by mode, I9) ----
    # Existence and maturity are public; fill content and cohort statistics need a
    # member identity (``id_token`` of a member, or a member's application key).

    def l8_mode(self) -> dict:
        return self._request("GET", "/l8/mode")

    def fills_available(self, *blocks: str) -> dict:
        """Public metadata: per block, how many fills exist and at which maturity."""
        return self._request("GET", "/l8/availability", params={"block": ",".join(blocks) or None})

    def fills(self, *, block: str | None = None, maturity: str | None = None, kind: str | None = None, limit: int = 200) -> dict:
        """Members: fill content for a block (403 ``members_only`` in agnostic mode)."""
        return self._request("GET", "/l8/fills", params={"block": block, "maturity": maturity, "kind": kind, "limit": limit})

    def fill(self, fill_id: str) -> dict:
        return self._request("GET", f"/l8/fills/{fill_id}")

    def benchmarks(self, *, cohort: str | None = None, metric: str | None = None, block: str | None = None) -> dict:
        """Members: k ≥ 5 cohort aggregates with Laplace noise — never an individual observation."""
        return self._request("GET", "/l8/benchmarks", params={"cohort": cohort, "metric": metric, "block": block})

    def benchmark_metrics(self) -> dict:
        return self._request("GET", "/l8/metrics")

    def submit_benchmark(self, cohort_key: str, metric: str, value: float, *, block_id: str | None = None) -> dict:
        """Members: one opt-in observation, stored under an HMAC of your identity and only ever published inside a k ≥ 5 aggregate."""
        return self._request("POST", "/l8/benchmarks/inputs", body={"cohort_key": cohort_key, "metric": metric, "value": value, "block_id": block_id})

    def iter_feed(self, *, since: str | None = None, page: int = 100) -> Iterator[dict]:
        seen: set[str] = set()
        for entry in self.feed(since=since, limit=page)["entries"]:
            key = f"{entry['layer']}:{entry['id']}"
            if key not in seen:
                seen.add(key)
                yield entry

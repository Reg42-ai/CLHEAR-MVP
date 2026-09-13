# clhear (Python SDK)

Standard-library client for the CLHEAR public API.

```python
from clhear import Client

c = Client("https://clhear.org")                      # open: no key needed
bp = c.build("A UK retail equities broker holding client money")
print(bp["blueprint_id"], bp["total_ms"], bp["within_budget"])

node = c.node(bp["blueprint_id"])                     # why, history, who else
feed = c.feed(since="2026-01-01")                     # change feed with effective dates
gates = c.evals()                                     # published eval gates per layer

keyed = Client("https://clhear.org", app_id="key-…", secret="clh_…")   # issued at /build
print(keyed.layers())
```

The agnostic blueprint, every why-trail, the change feed and the eval gates
are open without a key (HLD v2 I9). Keys add release-pinned reads.

Contribute (HLD v2 §6) — identify the *person* with a Cognito id token, sign the
CLA once, then file. Nothing is written to the registry by filing: checks, a
fleet re-derivation and two reviewers come first, and the change ships in the
next release with your handle and its impact.

```python
me = Client("https://clhear.org", id_token="eyJ…")   # from /auth (Cognito)
me.sign_cla()
con = me.contribute("correction", {"value": "Apply customer due diligence"},
                    target_ref="OBL:uksi/2017/692#regulation-27", field="title",
                    evidence=[{"url": "https://www.legislation.gov.uk/uksi/2017/692/regulation/27"}],
                    rationale="title paraphrases the heading too loosely")
print(con["id"], con["status"], [c["check"] for c in con["checks"] if not c["ok"]])
print(me.contribution(con["id"])["rederivation"])   # agree / disagree / unverified, once the fleet ran
```

Licence: Apache-2.0. Published from the `clhear` repository by the release
pipeline (item 13); this copy is the source of truth.

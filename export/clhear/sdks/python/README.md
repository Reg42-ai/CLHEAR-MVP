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

Licence: Apache-2.0. Published from the `clhear` repository by the release
pipeline (item 13); this copy is the source of truth.

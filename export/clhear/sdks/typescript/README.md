# @clhear/sdk (TypeScript)

Fetch-based client for the CLHEAR public API. Works in Node 18+, browsers, Deno and Bun.

```ts
import { ClhearClient } from "@clhear/sdk";

const c = new ClhearClient({ baseUrl: "https://clhear.org" });   // open: no key needed
const bp = await c.build("A UK retail equities broker holding client money");
console.log(bp.blueprint_id, bp.total_ms, bp.within_budget);

for await (const step of c.stream(bp.profile_id!)) console.log(step.layer, step.title);

const keyed = new ClhearClient({ baseUrl: "https://clhear.org", appId: "key-…", secret: "clh_…" }); // issued at /build
console.log(await keyed.layers());
```

The agnostic blueprint, every why-trail, the change feed and the eval gates
are open without a key (HLD v2 I9). Keys add release-pinned reads.

Licence: Apache-2.0. Published from the `clhear` repository by the release
pipeline (item 13); this copy is the source of truth.

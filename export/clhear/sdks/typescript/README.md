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

Contribute (HLD v2 §6) — identify the *person* with a Cognito id token, sign the
CLA once, then file. Filing never writes the registry: checks, a fleet
re-derivation and two reviewers come first; the change ships in the next
release with your handle and its impact.

```ts
const me = new ClhearClient({ baseUrl: "https://clhear.org", idToken: "eyJ…" });
await me.signCla();
const con = await me.contribute({ kind: "correction", proposed: { value: "Apply customer due diligence" },
  target_ref: "OBL:uksi/2017/692#regulation-27", field: "title",
  evidence: [{ url: "https://www.legislation.gov.uk/uksi/2017/692/regulation/27" }] });
console.log(con.id, con.status, con.checks.filter((c) => !c.ok));
```

L8 fills and benchmarks (HLD v2 §4.8) — *that* a block carries fills, and how
mature they are, is public; the fill text and the k ≥ 5, noise-protected cohort
statistics are member content. Members identify with their id token.

```ts
await client.fillsAvailable("BLK-000002");                 // public metadata
const member = new ClhearClient({ baseUrl: "https://clhear.org", idToken: "eyJ…" });
await member.fills({ block: "BLK-000002" });
await member.benchmarks({ metric: "cdd_refresh_days" });
await member.submitBenchmark("UK|payments|retail", "cdd_refresh_days", 365);
```

Licence: Apache-2.0. Published from the `clhear` repository by the release
pipeline (item 13); this copy is the source of truth.

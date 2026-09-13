# Deprecation policy

Version 1.0 · licence CC BY 4.0

Nothing in CLHEAR is deleted (invariant I2). Deprecation is the orderly closing of a thing's validity window while its record stays readable and citable.

## What can be deprecated

| Thing | Trigger | What happens |
|---|---|---|
| An obligation | its clause is repealed, or a revision changes its duty | `valid_to` is set on the effective date; a `revoked` change event names the L1 cause; a successor inherits the canonical thread via a supersession edge |
| A block, activity or ontology entry | harmonised into a canonical entry, or no longer required by any live obligation | `valid_to` set; `canonical_id` points to the survivor; edges are re-issued to the canonical entry |
| A schema field | replaced by a structured field | the field stays in the schema for at least two minor schema versions with `deprecated: true` and a `replaced_by`; then it becomes optional and stops being populated; it is never removed from historical snapshots |
| An API endpoint | replaced by a versioned successor | announced in release notes; served with a `Deprecation` and `Sunset` header for at least 180 days; then returns `410 Gone` with a pointer |
| A golden case | the law changed, or the case was wrong | marked `retired` with the reason and the release; scores exclude retired cases; the case file keeps it |
| A conformance level definition | program revision | assessed marks keep the definition they were assessed against for their validity period |
| A model in a derivation ladder | procurement or quality reasons | the manifest of the last release using it is the reference; affected records carry a new `method` version |

## Notice periods

- Schema and API deprecations: announced in a release note and an RFC at least 180 days before sunset.
- Data deprecations (obligations, blocks) follow the law's effective dates; there is no notice period beyond the change feed, which publishes detection date and effective date separately.
- Governance document deprecations: 30-day public RFC.

## Identifiers

Stable identifiers (`OBL-`, `BLK-`, `PRF-`, `ACT-`, `BLU-`, `RSK-`, `FIL-`, `CON-`) are never reused. A deprecated identifier resolves forever to its last version and its successor, if any.

## Where to look

`GET /explore/node/{id}` shows the validity window, the why-trail and the successor. The change feed (`/watch`) and the Atom feed carry every deprecation as an entry.

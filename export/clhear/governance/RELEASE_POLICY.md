# Release policy

Version 1.0 · licence CC BY 4.0

## Cadence and naming

- A release is a dated, immutable snapshot named `YYYY.MM.DD`. Nightly derivation runs every day; a release is cut when the gates pass, at least weekly.
- Two releases on one day are not cut; a second cut waits for the next day.
- Every release records a delta against the previous release (counts per layer, layers added or removed).

## What a release contains

1. The record snapshot per layer under `snapshots/<release>/`.
2. `manifest.json`: layers published, layers reserved (below gate), gate statuses, the frozen model manifest (model ids and versions used to derive), the content hash, the Sigstore signature and the SBOM reference, the licences, and the community contributions shipped with attribution and impact.
3. `evals/<release>.json`: every suite's score.
4. `RELEASE_NOTES/<release>.md`: what changed, who contributed, what it reached.
5. `vault/<release>/`: the Obsidian pack.

## Gates (invariant I10)

A layer appears in `layers` only when every suite in its gate passed on this release. A layer that drops below its gate is **reserved**: it is listed in `reserved_layers`, its previous release stays available, an alarm is raised and the maintainers of the cluster are paged. Reserved layers are never silently re-published: they return when the gate passes again and the release notes say so.

Platform suites (record integrity, layer order, never-list) gate the release as a whole.

## Signing and verification

Releases are signed with Sigstore (keyless, the release workflow's OIDC identity). `scripts/verify_release.py` in the reference implementation verifies signature, content hash, frozen model ids and gate statuses; the same check runs before the public repo export.

## Model manifest

Model ids and versions are frozen per release. A change of model in any derivation class is a release note item and a new `method` version on the affected records; scores and characteristics are re-derived, previous versions remain on the record.

## Hotfixes

A hotfix (a wrong effective date, a leaked restricted text) is a new dated release, never an edit of an existing one. Restricted text discovered in a public artefact is removed from the *next* release and the incident is disclosed in its release notes; the offending release is withdrawn from `latest` but its manifest remains listed as withdrawn.

## Support window

The three most recent releases are served by the API by release id. Older releases remain downloadable from the repository history.

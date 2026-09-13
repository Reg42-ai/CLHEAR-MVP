# Business Continuity and Disaster Recovery

Owner: Maintainers (on-call). Targets follow HLD v2 §7.1.

## Objectives

| Component | RPO | RTO | Mechanism |
|---|---|---|---|
| Data lake (immutable source texts) | 0 (Object Lock, versioned) | 1 h | S3 Object Lock; cross-region replication (`infra/s3.tf`, `replication_enabled`) |
| Record (Aurora Postgres, L0–L8) | ≤ 5 min (continuous backup) | 4 h | Aurora automated backups, 35-day retention (`infra/rds.tf`); point-in-time restore |
| Neo4j projection | rebuildable | 2 h | derived from the record (`scripts/rebuild_projection`, nightly); EFS is a cache, not a source |
| Web tier | stateless | 30 min | Terraform re-apply; API Gateway + Lambda/ECS from ECR |
| Public repository and releases | 0 | n/a | GitHub + signed release artefacts; any consumer can verify (`scripts/verify_release.py`) |

## Drills

Two scheduled drills run **nightly**, both implemented by `app/clhear/platform/dr.py`
(`python -m app.clhear.platform.dr run`, also `scripts/dr_drill.py`):

* **In-VPC** — EventBridge (`infra/eventbridge.tf`, `dr_drill`, 04:00 UTC) sends
  `DrDrillRequested` to the L0 fleet, which `pg_dump`s the Aurora record, `pg_restore`s
  it into the `clhear_drill` database on the same cluster, rebuilds the graph projection
  into the Neo4j `drill` database and samples the cross-region datalake replica.
* **Out-of-band** — `.github/workflows/dr_drill.yml` (04:00 UTC) restores the latest
  *published release snapshot* into a fresh Postgres and Neo4j (service containers) —
  proving the artefact any adopter holds is itself restorable — and keeps the report
  90 days as evidence.

Each drill asserts:

1. **record** — every layer table restored row-for-row (counts equal), the migration
   ledger identical, and every `why_trail_id` on the restored rows resolves (I2);
2. **graph** — the projection rebuilt from the restored record checksum-matches the
   projection of the live record (derived state is reproducible from the record alone);
3. **datalake** — the replication rule is enabled and targets the replica, the replica is
   versioned, and a sample of source keys is present with equal ETags;
4. **objectives** — measured RPO ≤ 24 h and RTO ≤ 4 h (`dr.TARGETS`).

Results land in `l0_platform.dr_drills` (append-only) and are published as
`clhear.l0.dr_drill`; `/status.json` exposes the latest drill (`dr`) and the
`dr_drilled` SLO is missed when no passing drill is younger than 48 h. `DrDrillPassed`
missing or 0 for 48 h raises the `dr-drill-failed` alarm (`infra/observability.tf`).
`tests/test_dr_drill.py` proves the drill itself catches a truncated restore, an orphaned
why-trail and each replica failure mode. A failed drill is a SEV3 incident
(`INCIDENT_RESPONSE.md`).

Monthly, one drill is run **with a human**: an on-call maintainer follows the runbook
end to end, times it, and records RTO achieved in the drill log.

## Runbook (region loss)

1. Declare SEV1; status page to `outage`.
2. `terraform apply` in the replica region with `replication_enabled=false` and the
   replica bucket as the data lake.
3. Restore Aurora from the latest cross-region automated backup copy.
4. Deploy the web tier; run migrations; rebuild the projection.
5. Re-point DNS (`infra/webui_domain.tf`).
6. Verify `/status.json` operational; announce.

## Dependencies outside our control

AWS regional availability, GitHub, Discourse, Bedrock through Reg42 Infer. Each is
listed with its fallback in `VENDOR_MANAGEMENT.md`.

# HLD v2 requirement trace

Every mechanism in [`CLHEAR_HLD_v2.md`](CLHEAR_HLD_v2.md) §1.1, §2, §3, §4, §8 and §9
gets a stable requirement id `CLHEAR-<layer>.<n>` (invariant I11), the code path that
implements it, the test that proves it, and a status. Status values: `done`, `partial`,
`todo`, `blocked` (needs something outside this repo — see the "Blocked" section).

This file is acceptance evidence. A build item in §8 is not complete until every row it
owns is `done` or `blocked` with a named blocker. `scripts/trace_check.py` fails CI if a
row references a code path or test that does not exist.

## §2 Invariants

| Req | Invariant | Code | Test | Status |
|---|---|---|---|---|
| CLHEAR-0.1 | I1 layers derived strictly in order; L(n) reads only ≤ L(n−1) | `app/clhear/layers.py` (`LAYER_CATALOG[*].derivation.inputs`), `app/clhear/platform/record.py` (`assert_layer_inputs`, `WhyTrail.input_layers`) | `tests/test_record.py::test_layer_input_guard`, `tests/test_record.py::test_write_rejects_lower_layer_reading_higher` | done |
| CLHEAR-0.2 | I2 every node/edge versioned and dated; nothing deleted, only invalidated | `app/clhear/platform/shared_schema.py` (`shared_columns`), `app/clhear/platform/record.py` (`invalidate`, `rebuild_projection`, `drop_fts_rows`), `migrations/m0008_shared_schema.py` | `tests/test_record.py::test_no_delete_anywhere`, `tests/test_record.py::test_invalidate_sets_valid_to`, `tests/test_record.py::test_rebuild_projection_only_for_projection_tables` | done |
| CLHEAR-0.3 | I3 every determination has a why-trail; no trail, no write | `app/clhear/platform/record.py` (`write` raises `WhyTrailRequired`, `why_trails` table) | `tests/test_record.py::test_write_requires_why_trail`, `tests/test_record.py::test_write_populates_shared_columns_and_why_trail` | done |
| CLHEAR-0.4 | I4 full AI determination, HITL by exception (confidence < threshold, modification requests, re-derive with human edits) | `app/clhear/platform/console.py`, `app/clhear/platform/record.py` (`LOW_CONFIDENCE_THRESHOLDS`) | `tests/test_console.py` | todo |
| CLHEAR-0.5 | I5 agnostic store contains no organization data; automated scan per release | `app/clhear/platform/agnostic_scan.py`, `.github/workflows/release.yml` | `tests/test_never_list.py::test_agnostic_scan_flags_pii_and_org_identifiers`, `tests/test_never_list.py::test_agnostic_scan_of_store_is_clean` | partial |
| CLHEAR-0.6 | I6 Bedrock-only inference via Reg42 Infer; frozen model ids; manifest per release | `app/clhear/platform/gateway.py` (`InferProvider`), `app/clhear/platform/manifest.py`, `app/clhear/platform/task_classes.py` | `tests/test_infer_provider.py`, `tests/test_release_verify.py::test_model_manifest_complete_and_clean` | done |
| CLHEAR-0.7 | I7 Postgres record, Neo4j query graph, vectors an index; all rebuildable from Postgres | `infra/rds.tf`, `app/clhear/db.py` (`all_schemas`, pgvector extension), `scripts/migrate_snapshot_to_postgres.py`, `infra/neo4j.tf`, `app/clhear/platform/graph.py` | `tests/test_graph.py` | partial |
| CLHEAR-0.8 | I8 rights before text | `app/clhear/l1/rights.py` (`rights_for`, `record`, `republishable`), `app/clhear/l1/pipeline.py` (`ensure_source`, `rights_basis_for`), `app/clhear/v1/l1.py` (`clause_detail` withholds text) | `tests/test_l1_synthetic_amendment.py::test_amendment_emits_change_event_with_effective_date_and_clause_ids`, `tests/test_l1_publisher_adapters.py::test_every_publisher_adapter_declares_rights_and_schedule`, `tests/test_v1_api.py::test_clause_detail_serves_spans_and_respects_rights` | done |
| CLHEAR-0.9 | I9 open by mode not by layer | `app/clhear/platform/mode.py` | `tests/test_mode.py` | todo |
| CLHEAR-0.10 | I10 evals gate publication; drift below gate freezes layer + alarms | `app/clhear/platform/gates.py` (`gate_status`, `freeze_below_gate`), `app/clhear/releases.py` (`_gate_layers`), `infra/cloudwatch.tf` (`layer_below_gate`) | `tests/test_release_verify.py::test_layer_below_gate_is_reserved_not_published`, `tests/test_release_verify.py::test_fresh_install_publishes_l1_unverified_and_verify_flags_it` | done |
| CLHEAR-0.11 | I11 stable identifiers `CLHEAR-<layer>.<n>`, `OBL-` `BLK-` `PRF-` `ACT-` `BLU-` `RSK-` `FIL-`, never reused | `app/clhear/platform/ids.py` (`next_id`, `id_sequences`) | `tests/test_ids.py::test_next_id_is_monotonic_per_prefix`, `tests/test_ids.py::test_every_hld_prefix_is_registered` | done |
| CLHEAR-0.12 | I12 contributions never write directly | `app/clhear/community.py` (proposal path), `app/clhear/platform/contributions.py` | `tests/test_contributions.py` | todo |

## §3 L0 platform plane

| Req | Component | Code | Test | Status |
|---|---|---|---|---|
| CLHEAR-0.20 | Record store: Postgres, one schema per layer, bi-temporal columns | `infra/rds.tf`, `app/clhear/db.py` (`all_schemas`), `migrations/m0008_shared_schema.py`, `scripts/migrate_snapshot_to_postgres.py` | `tests/test_record.py::test_all_tables_have_shared_columns` | done |
| CLHEAR-0.21 | Query graph: Neo4j Community projection, rebuilt nightly | `infra/neo4j.tf`, `app/clhear/platform/graph.py` | `tests/test_graph.py` | todo |
| CLHEAR-0.22 | Retrieval index: pgvector | `migrations/m0016_pgvector.py`, `app/clhear/l1/retrieval.py` | `tests/test_retrieval.py` | todo |
| CLHEAR-0.23 | Object store: S3 Object Lock, cross-region replica | `infra/s3.tf` (`aws_s3_bucket_replication_configuration.datalake`) | terraform validate | done |
| CLHEAR-0.24 | Events: EventBridge bus `clhear.<layer>.<event>` (`derived`, `changed`, `invalidated`, `below_gate`) | `app/clhear/platform/events.py` (`publish_layer_event`, `EventBridgeTransport`), `infra/eventbridge.tf` (`aws_cloudwatch_event_bus.clhear`, fan-out rules, archive) | `tests/test_events_bus.py::test_publish_layer_event_relays_to_bus_and_queue` | done |
| CLHEAR-0.25 | Workers: one task definition per layer fleet, scale 0 → N | `infra/ecs.tf` (`aws_ecs_task_definition.fleet`, `aws_ecs_service.fleet`), `infra/sqs.tf` (`aws_sqs_queue.fleet`), `app/clhear/workers.py` | terraform validate | done |
| CLHEAR-0.26 | Inference: Reg42 Infer with CLHEAR task classes | `app/clhear/platform/router.py`, `handoff/reg42-infra/tasks.clhear.yaml` | `tests/test_infer_route_explain.py` | done |
| CLHEAR-0.27 | Evals: Langfuse self-hosted + per-layer golden sets; public dashboard reads summary table | `infra/langfuse.tf`, `app/clhear/platform/evals.py` (`publish_summary`), `clhear-evals/` | `tests/test_gates.py::test_summary_table_published` | todo |
| CLHEAR-0.28 | Approval console at `/console` | `app/clhear/platform/console.py`, `app/clhear/web/console.html` | `tests/test_console.py` | todo |
| CLHEAR-0.29 | Release pipeline: nightly derive → evals → snapshot → Sigstore → publish; `YYYY.MM.DD`; daily deltas | `.github/workflows/release.yml`, `app/clhear/releases.py` (`release_id_for`, `build_manifest`, `_delta`), `app/clhear/fleets.py` (`main`), `scripts/verify_release.py` | `tests/test_release_verify.py::test_publish_and_verify_release`, `tests/test_release_verify.py::test_delta_against_previous` | done |
| CLHEAR-0.30 | Identity: Cognito public users, Google SSO Reg42, API keys per org, SAML enterprise | `infra/cognito.tf`, `app/clhear/app_auth.py`, `app/clhear/api_keys.py` | `tests/test_api_keys.py` | todo |
| CLHEAR-0.31 | Observability: Prometheus + Grafana, GlitchTip, CloudWatch, status page; freshness and gate status public | `infra/observability.tf`, `app/clhear/platform/metrics.py`, `status/` | `tests/test_metrics.py` | todo |
| CLHEAR-0.32 | Public repo `clhear`: standard, schema, vault packs, evals harness, SDKs | `export/clhear/`, `app/clhear/platform/exporter.py` | `tests/test_exporter_public.py` | todo |
| CLHEAR-0.33 | Shared schema columns on every layer table | `app/clhear/platform/shared_schema.py` (`SHARED_COLUMN_NAMES`, `attach_shared_columns`), `app/clhear/platform/record.py` (`layer_tables`) | `tests/test_record.py::test_all_tables_have_shared_columns` | done |

## §4 Layers

| Req | Layer / mechanism | Code | Test | Status |
|---|---|---|---|---|
| CLHEAR-1.1 | L1 schema: sources (rights basis, family root), source_versions, clauses (span offsets, normative), families, citations, rights_records, watchlists | `app/clhear/l1/models.py`, `migrations/m0009_l1_hardening.py`, `app/clhear/l1/spans.py` | `tests/test_l1_synthetic_amendment.py::test_spans_index_the_canonical_text_and_normative_flags`, `tests/test_l1_publisher_adapters.py` | done |
| CLHEAR-1.2 | L1 fleet: fetchers per publisher, family completeness, change detectors with effective dates, rights recorder; `clhear.l1.changed` | `app/clhear/l1/adapters/publisher.py`, `app/clhear/l1/adapters/fca_handbook.py`, `app/clhear/l1/adapters/sec_edgar.py`, `app/clhear/l1/adapters/standards_bodies.py`, `app/clhear/l1/families.py` (`mine_citations`, `family_scorecard`), `app/clhear/l1/change_detect.py`, `app/clhear/l1/pipeline.py`, `app/clhear/l1/rights.py`, `infra/eventbridge.tf` | `tests/test_l1_synthetic_amendment.py`, `tests/test_l1_publisher_adapters.py`, `tests/test_wave1_and_schedule.py` | done |
| CLHEAR-1.3 | L1 gates: byte fidelity 100 %, family completeness ≥ 99 %, currency ≤ 24 h, clause boundary F1 ≥ 0.98 | `app/clhear/platform/evals.py` (`e1_fidelity`, `e7_closure`, `l1_family_completeness`, `l1_currency`, `l1_boundary_f1`), `app/clhear/platform/gates.py` (`LAYER_GATES["L1"]`), `clhear-evals/l1/boundary/` | `tests/test_l1_evals.py`, `tests/test_dummy_fleet_rehearsal.py` | done |
| CLHEAR-1.4 | L1 API GET /l1/sources, /l1/sources/{id}/versions, /l1/clauses/{id}, /l1/changes, /l1/families, /l1/scorecard, GET/POST /l1/watchlists + the /l1 browser UI | `app/clhear/v1/l1.py`, `app/clhear/web/l1.html`, `app/clhear/l1/routes.py` (`l1_browser`) | `tests/test_v1_api.py` | done |
| CLHEAR-1.5 | L1 starter corpus adapters (MiFIR/MiFID II, MAR, MLRs, FCA Handbook, SEC/FINRA via EDGAR, FATCA, GDPR, NIST) | `app/clhear/l1/adapters/fca_handbook.py`, `app/clhear/l1/adapters/sec_edgar.py`, `app/clhear/l1/adapters/standards_bodies.py`, `app/clhear/l1/starter_corpus.py`, `app/clhear/l1/registry_etoro.py` | `tests/test_starter_corpus.py`, `tests/test_l1_publisher_adapters.py` | done |
| CLHEAR-2.1 | L2 schema: obligations (stable OBL-000001 ids, structured who/what/when fields, effective dates), asserts (span, strength), equivalences, supersessions, change_events, obligation_reviews | `app/clhear/derived_models.py`, `app/clhear/l2/models.py`, `app/clhear/l2/registry.py`, `migrations/m0010_l2_registry.py` | `tests/test_l2_registry.py` | done |
| CLHEAR-2.2 | L2 fleet: extractors (leaf-clause anchored, why-trail per write), consolidators (dedupe within jurisdiction, equivalence across), change inferencers (clhear.l1.changed -> added/updated/revoked with effective dates, supersession, expiry), second-model reviewers (l2.review -> proposals), grounded structured refinement | `app/clhear/l2/extract.py`, `app/clhear/l2/triage.py`, `app/clhear/l2/dedupe.py`, `app/clhear/l2/change.py`, `app/clhear/l2/review.py`, `app/clhear/l2/structured.py`, `app/clhear/workers.py` (`handle_l1_changed`), `app/clhear/fleets.py` | `tests/test_l2_registry.py` | done |
| CLHEAR-2.3 | L2 gates: coverage ≥ 99 %, precision ≥ 95 %, dedupe < 1 %, change inference ≥ 95 % (golden set replayed) + basis integrity | `app/clhear/platform/evals.py` (`l2_coverage`, `l2_precision`, `l2_dedupe`, `l2_change_inference`), `app/clhear/platform/gates.py`, `clhear-evals/l2/change_events/core.json` | `tests/test_l2_registry.py` (`test_l2_gate_suites`) | done |
| CLHEAR-2.4 | L2 API GET /l2/obligations, /l2/obligations/{id} (+/history, /why), POST modification-requests + reviews, /l2/changes, /l2/equivalences, /l2/compare, /l2/scorecard + the /l2 registry browser UI | `app/clhear/v1/l2.py`, `app/clhear/web/l2.html`, `app/main.py` | `tests/test_l2_registry.py` | done |
| CLHEAR-3.1 | L3 schema: blocks (8 kinds: System, Document, Role, Configuration, Process, Workflow, Asset, Body; purpose; canonical_id harmonisation thread), requires (obligation -> block with rationale span, method, text hash), characteristics (fixed schema per kind; backed / not_specified / unbacked with backing span), l3_kinds registry | `app/clhear/l3/kinds.py`, `app/clhear/derived_models.py`, `migrations/m0011_l3_blocks.py`, `app/clhear/curated/l3_building_blocks.json` | `tests/test_l3_blocks.py` | done |
| CLHEAR-3.2 | L3 fleet: decomposers (curated anchors -> explicit requires edges; deterministic duty-sentence kind cue + harmonised name, reuse-or-mint BLK-000001), characterizers (regex extraction backed by spans; grounded LLM fill, ungrounded values kept as unbacked), harmonizers (same-kind name similarity -> canonical block, edges re-issued, dropped block invalidated not deleted), propagators (clhear.l2.changed: revoked -> edges closed, updated -> edges re-stamped + backed characteristics reopened), L6 composer covers via requires edges | `app/clhear/l3/decompose.py`, `app/clhear/l3/characterize.py`, `app/clhear/l3/harmonize.py`, `app/clhear/l3/generate.py`, `app/clhear/workers.py` (`handle_l2_changed`), `app/clhear/fleets.py`, `app/clhear/l6/composer.py` | `tests/test_l3_blocks.py` | done |
| CLHEAR-3.3 | L3 gates: 100 % obligation -> block, characteristic completeness ≥ 95 %, expert precision ≥ 92 % (Eval Studio votes), reuse ratio + explosion check | `app/clhear/platform/evals.py` (`l3_completeness`, `l3_characteristics`, `l3_reuse`, `l3_precision`), `app/clhear/platform/gates.py` | `tests/test_l3_blocks.py` (`test_l3_gate_suites`) | done |
| CLHEAR-3.4 | L3 API GET /l3/kinds, /l3/blocks, /l3/blocks/{id} (+/history, /why; characteristics, backing obligations with rationale spans, needed-by profiles), /l3/obligations/{id}/blocks, /l3/scorecard, POST modification-requests + the /l3 catalogue browser UI | `app/clhear/v1/l3.py`, `app/clhear/web/l3.html`, `app/main.py` | `tests/test_l3_blocks.py` | done |
| CLHEAR-4.1 | L4 schema: licences (register key / URL / ref, regime, aliases, clause anchors, canonical_id), products_services, client_types, channels, profiles (PRF-000001, fingerprint, validity, source, status), permits (licence -> product with basis), applies_to (obligation -> predicate with basis, rationale, method, text hash), validity_rules (if / requires / forbids, severity); shared bi-temporal columns; reviewed register snapshot | `app/clhear/derived_models.py`, `migrations/m0012_l4_ontology.py`, `app/clhear/curated/l4_ontology.json`, `app/clhear/curated/l4_attribute_schema.json` | `tests/test_l4_ontology.py` | done |
| CLHEAR-4.2 | L4 fleet: register-backed ontology builder with live cross-check adapters (FCA, ESMA, EBA, SEC/FINRA, FinCEN, NFA) and why-trails, re-versioning / invalidation on snapshot change, `clhear.l4.changed`; deterministic applicability predicates (jurisdiction / subject / condition) + grounded quote-required LLM read, L2 change re-stamping via the worker; validator (schema, closed-world values, authorisation jurisdiction, permits, validity rules), fingerprinted profile store, profile re-validation propagator; guided builder that only offers valid permutations, permutation explorer, similar profiles; L6 composer triggers via applies_to and shares the predicate language | `app/clhear/l4/{ontology,registers,predicates,validate,builder}.py`, `app/clhear/workers.py`, `app/clhear/fleets.py`, `app/clhear/l6/composer.py`, `app/clhear/platform/router.py` | `tests/test_l4_ontology.py` | done |
| CLHEAR-4.3 | L4 gates: validity ≥ 99 % on golden profiles + impossible permutations with register provenance, applicability P/R ≥ 95 % against golden predicates + referential integrity of stored edges; wired into the nightly gate loop and the L4 gate tuple in gates.py | `app/clhear/platform/evals.py` (`l4_validity`, `l4_applicability`), `app/clhear/platform/gates.py`, `clhear-evals/l4/profiles/core.json`, `clhear-evals/l4/applicability/core.json` | `tests/test_l4_ontology.py` | done |
| CLHEAR-4.4 | L4 API + browser under /l4: ontology (whole + per collection), licence page with register provenance / permits / rules / holders / why, profile validate + create (422 on impossible permutations) + list + page (validity, history, why) + obligations that apply + similar profiles, guided builder next-step, permutation explorer, obligation applies-to edges, scorecard; UI: guided validating builder, ontology and permutation explorers, profile page, scorecard | `app/clhear/v1/l4.py`, `app/clhear/web/l4.html`, `app/main.py`, `app/clhear/layers.py`, `app/clhear/layer_service.py` | `tests/test_l4_ontology.py` | done |
| CLHEAR-5.1 | L5 schema: activities with side (business / compliance; DB check), action type from the published vocabulary, canonical_id; implies (product / service -> business activity), operates (compliance activity -> block with obligation refs), mitigates (compliance activity -> business activity with obligation refs); the narrative metaphor for the two sides never appears in schema, vocabulary, curated table or API (lint) | `app/clhear/derived_models.py`, `app/clhear/l5/models.py`, `migrations/m0013_l5_junction.py`, `app/clhear/curated/l5_activities.json` | `tests/test_l5_junction.py` | done |
| CLHEAR-5.2 | L5 fleet: activity mappers (every live obligation -> a compliance activity by duty-text cue, else the activity operating the block it requires; trigger when-condition = the obligation's L4 applicability predicate; closed-world router refinement that chooses from existing activities or proposes one within the vocabulary and must quote the text), junction builder (implies from the reviewed product table, operates through triggered obligations' requires edges, mitigates lit by curated anchors / shared anchors / L4 product predicates; idempotent, re-versioning, invalidation, `clhear.l5.changed`), consistency checker (no orphan activity, no dangling endpoint, vocabulary + schema-only when-conditions), propagation (L2 revoked unlights / closes edges, added maps the source; L4 ontology change re-derives implies) | `app/clhear/l5/map.py`, `app/clhear/l5/check.py`, `app/clhear/workers.py` (`handle_l2_changed`, `handle_l4_changed`), `app/clhear/fleets.py` | `tests/test_l5_junction.py` | done |
| CLHEAR-5.3 | L5 gates: junction completeness 100 % (no orphan, no dangling edge, every live obligation mapped), golden mapping accuracy ≥ 92 % + golden activity maps, expert precision ≥ 92 % (Eval Studio votes); wired into the nightly gate loop and the L5 gate tuple | `app/clhear/platform/evals.py` (`l5_completeness`, `l5_mapping`, `l5_precision`), `app/clhear/platform/gates.py`, `clhear-evals/l5/mapping/core.json` | `tests/test_l5_junction.py` | done |
| CLHEAR-5.4 | L5 API + browser under /l5: vocabulary, activities (facets) and activity page (triggers resolved to obligations, implies / operates / mitigates, history, why), activity obligations, activity map for a stored L4 profile or ad-hoc attributes, obligation activities, edge lists, scorecard; UI: activity map (business side, compliance side, edges lit by obligation), catalogue, activity page, scorecard | `app/clhear/v1/l5.py`, `app/clhear/web/l5.html`, `app/main.py`, `app/clhear/layers.py`, `app/clhear/layer_service.py` | `tests/test_l5_junction.py` | done |
| CLHEAR-6.1 | L6 schema: blueprints, blueprint_items, minimality_proof | `app/clhear/derived_models.py` (`blueprints`, `blueprint_items`, `minimality_proofs`), `app/clhear/l6/models.py`, `migrations/m0014_l6_blueprints.py` | `tests/test_l6_blueprints.py::test_migration_backfills_legacy_rows_and_composes_stored_profiles`, `tests/test_l6_blueprints.py::test_leanest_complete_program_with_required_blocks_and_proof` | done |
| CLHEAR-6.2 | L6 fleet: set-cover composer, explainers, diff engine | `app/clhear/l6/composer.py` (`compose`, `_minimise`, `store_blueprint`), `app/clhear/l6/explain.py`, `app/clhear/l6/rationale.py`, `app/clhear/l6/diff.py`, `app/clhear/l6/check.py`, `app/clhear/workers.py` (`l6_on_changed`) | `tests/test_l6_blueprints.py::test_set_cover_picks_fewest_blocks_and_prunes_redundant_ones`, `tests/test_l6_blueprints.py::test_gaps_are_surfaced_and_explanations_pass_the_rubric`, `tests/test_l6_blueprints.py::test_diff_engine_recomposes_on_lower_layer_change_and_supersedes` | done |
| CLHEAR-6.3 | L6 gates: completeness 100 %, minimality, reference agreement ≥ 90 %, explanation ≥ 90 % | `app/clhear/platform/evals.py` (`l6_completeness`, `l6_minimality`, `l6_reference`, `l6_explanation`), `app/clhear/platform/gates.py` (`LAYER_GATES["L6"]`), `clhear-evals/l6/reference/core.json` | `tests/test_l6_blueprints.py::test_l6_gates_and_scorecard` | done |
| CLHEAR-6.4 | L6 API incl. OSCAL export | `app/clhear/v1/l6.py`, `app/clhear/interop/oscal.py`, `app/clhear/web/l6.html` | `tests/test_interop.py`, `tests/test_l6_blueprints.py::test_oscal_export_round_trips`, `tests/test_l6_blueprints.py::test_l6_api_and_browser` | done |
| CLHEAR-7.1 | L7 schema: risk_scores, enforcement_events | `app/clhear/l7/models.py`, `migrations/m0014_l7.py` | `tests/test_l7_risk.py` | todo |
| CLHEAR-7.2 | L7 fleet: enforcement ingestors/linkers, calibrated scorer with published weights | `app/clhear/l7/{enforcement,score}.py` | `tests/test_l7_risk.py` | todo |
| CLHEAR-7.3 | L7 gate: Brier on held-out year, linker precision ≥ 90 % | `app/clhear/platform/evals.py` (`l7_brier`, `l7_linker`) | `tests/test_l7_risk.py` | todo |
| CLHEAR-7.4 | L7 API | `app/clhear/v1/l7.py` | `tests/test_v1_api.py` | todo |
| CLHEAR-8.1 | L8 schema: fills, benchmark_aggregates (k ≥ 5, DP noise) | `app/clhear/l8/models.py`, `migrations/m0015_l8.py` | `tests/test_l8_fills.py` | todo |
| CLHEAR-8.2 | L8 fleet: fill generators, aggregators, reviewers, drift detectors | `app/clhear/l8/{fills,aggregate}.py` | `tests/test_l8_fills.py` | todo |
| CLHEAR-8.3 | L8 gates: rubric ≥ 85 %, traceability, re-identification test | `app/clhear/platform/evals.py` (`l8_reidentification`, `l8_traceability`) | `tests/test_l8_fills.py` | todo |
| CLHEAR-8.4 | L8 members-only API; public metadata only | `app/clhear/v1/l8.py`, `app/clhear/platform/mode.py` | `tests/test_l8_fills.py` | todo |

## §5 Interface

| Req | Mechanism | Code | Test | Status |
|---|---|---|---|---|
| CLHEAR-9.1 | Solon front door → blueprint < 60 s with progress narrative | `app/clhear/web/front_door.html`, `app/clhear/solon.py` | `tests/test_front_door.py` | todo |
| CLHEAR-9.2 | Explore: graph + list per layer; why, history, who else, request a change; cross-jurisdiction compare | `app/clhear/web/explore.html`, `app/clhear/v1/explore.py` | `tests/test_v1_api.py` | todo |
| CLHEAR-9.3 | Learn: tours, obligation of the week, playgrounds, learning path + badge, quizzes | `app/clhear/learn.py`, `app/clhear/web/learn.html` | `tests/test_learn.py` | todo |
| CLHEAR-9.4 | Watch: digest, public change feed, watchlists | `app/clhear/watch.py` | `tests/test_watch.py` | todo |
| CLHEAR-9.5 | Build on it: API keys, SDKs, OSCAL/JSON-LD, sandbox, public evals | `app/clhear/api_keys.py`, `export/clhear/sdks/`, `app/clhear/interop/` | `tests/test_api_keys.py`, `tests/test_interop.py` | todo |
| CLHEAR-9.6 | Design rules: evidence one click, WCAG 2.2 AA, dark/light, no paywall on agnostic blueprint | `app/clhear/web/theme.css`, `scripts/a11y_check.py` | `tests/test_front_door.py::test_no_paywall` | todo |

## §6 Community

| Req | Mechanism | Code | Test | Status |
|---|---|---|---|---|
| CLHEAR-10.1 | Roles: Reader, Contributor (CLA), Reviewer (two accept), Maintainer, Steering | `app/clhear/community_models.py`, `app/clhear/platform/contributions.py` | `tests/test_contributions.py` | todo |
| CLHEAR-10.2 | Contribution flow with automated checks, re-derivation, attribution, impact count | `app/clhear/platform/contributions.py` | `tests/test_contributions.py` | todo |
| CLHEAR-10.3 | Governance artefacts, CLA, CoC, licences, trademark policy | `export/clhear/governance/` | `tests/test_exporter_public.py` | todo |
| CLHEAR-10.4 | Conformance program CL1–CL4 | `export/clhear/conformance/`, `app/clhear/conformance.py` | `tests/test_conformance.py` | todo |

## §7 Trust

| Req | Mechanism | Code | Test | Status |
|---|---|---|---|---|
| CLHEAR-11.1 | Audit log of licensed-text reads and every write | `app/clhear/platform/audit.py` | `tests/test_audit.py` | todo |
| CLHEAR-11.2 | Signed releases (Sigstore), SBOM, pinning | `.github/workflows/release.yml` (cosign sign-blob, syft), `scripts/verify_release.py`, `app/clhear/releases.py` (`pin_release`) | `tests/test_release_verify.py::test_publish_and_verify_release` | done |
| CLHEAR-11.3 | Status page with SLOs; DR drills | `status/`, `.github/workflows/dr_drill.yml` | — | todo |
| CLHEAR-11.4 | Rights and sourcing (7.3), similarity guard in CI | `app/clhear/l1/rights.py`, `app/clhear/l1/guard.py`, `.github/workflows/ci.yml` | `tests/test_rights.py` | todo |
| CLHEAR-11.5 | Model governance: frozen ids, manifest, second-model review, low-confidence human review | `app/clhear/platform/manifest.py` (`check_manifest`, `freeze_from_infer`), `app/clhear/platform/record.py` (`needs_human`), `app/clhear/l2/review.py`, `app/clhear/platform/console.py` | `tests/test_release_verify.py::test_model_manifest_rejects_cn_origin_in_derivation` | partial |
| CLHEAR-11.6 | Instance-mode contract with Reg42 OS | `docs/INSTANCE_MODE_CONTRACT.md`, `app/clhear/instance_contract.py` | `tests/test_agnostic_scan.py` | todo |

## §9 Never-list (CI lint)

| Req | Rule | Test | Status |
|---|---|---|---|
| CLHEAR-12.1 | No deletion of nodes/edges | `tests/test_record.py::test_no_delete_anywhere` | done |
| CLHEAR-12.2 | No inference outside Reg42 Infer | `tests/test_never_list.py::test_only_infer_provider_in_prod` | done |
| CLHEAR-12.3 | No LLM call outside `router.run` | `tests/test_never_list.py::test_no_gateway_calls_outside_router` | done |
| CLHEAR-12.4 | Offense/defense never schema terms | `tests/test_never_list.py::test_no_offense_defense_schema_terms` | todo |
| CLHEAR-12.5 | No Chinese-origin weights in derivation classes | `tests/test_never_list.py::test_derivation_ladders_are_procurement_clean` | done |
| CLHEAR-12.6 | No public disclosure before filing confirmed | `tests/test_never_list.py::test_disclosure_gate` | todo |
| CLHEAR-12.7 | No verbatim text without rights basis | `tests/test_rights.py` | todo |
| CLHEAR-12.8 | No layer reads a higher layer | `tests/test_record.py::test_layer_input_guard` | done |

## Blocked (needs action outside this repo)

| Item | Blocker | Delivered here instead |
|---|---|---|
| Infer `tasks.yaml` in `reg42-infra` | repo not readable by the build agent | `handoff/reg42-infra/tasks.clhear.yaml` + contract test |
| Public `clhear` GitHub repo | must be created by an org owner | `export/clhear/` ready to push once `CLHEAR_PUBLIC_DISCLOSURE_CONFIRMED=true` |
| Solon entity behaviour | `SOLON_PLAN.md` not accessible | `app/clhear/solon.py` guided front door on Reg42 UI tokens |
| Meridian Markets Annex G + three anonymized profiles | not provided | `clhear-evals/l4/golden_profiles.json`, `clhear-evals/l6/reference_programs.json` seeded with placeholders marked `golden=false` |
| Discourse, beehiiv, pen test vendor, Cognito SAML IdP metadata | accounts / vendors | Terraform + docs prepared; values in `infra/variables.tf` |

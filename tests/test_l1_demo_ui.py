"""Source-reader identity regressions; no generated corpus or live services.

The actual no-build script runs in a small hook/template harness. Browser visual
checks cover React/htm separately; these check immutable links and access states.
"""
from pathlib import Path
import shutil
import subprocess

import pytest

PAGE = Path(__file__).resolve().parents[1] / "app/clhear/web/sources.html"
NODE = shutil.which("node")
pytestmark = pytest.mark.skipif(NODE is None, reason="Node is required to check reader JavaScript")


def run_js(assertions: str) -> None:
    harness = r'''
const fs = require("node:fs"), vm = require("node:vm"), assert = require("node:assert/strict");
const page = fs.readFileSync(process.argv[1], "utf8");
const script = page.split('<script type="module">')[1].split('</script>')[0]
  .replace(/^import .*;$/gm, "")
  .replace(/createRoot\(document\.getElementById\("root"\)\)\.render\(React\.createElement\(App\)\);/, "");
let states = [];
const flatten = v => Array.isArray(v) ? v.map(flatten).join("") : v == null || v === false || typeof v === "function" ? "" : String(v);
const context = {
  assert, URLSearchParams, console, setTimeout, clearTimeout, setInterval, clearInterval,
  React: {createElement: () => null}, htm: {bind: () => (strings, ...values) => strings.reduce((s, part, i) => s + part + flatten(values[i]), "")},
  useState: value => [states.length ? states.shift() : typeof value === "function" ? value() : value, () => {}],
  useEffect: () => {}, useRef: value => ({current: value}),
  location: {search: "", origin: "http://localhost"}, history: {replaceState: () => {}},
  navigator: {}, localStorage: {getItem: () => null, setItem: () => {}},
  fetch: async () => { throw Error("Unexpected network request"); },
  setStates: values => { states = values; },
};
vm.createContext(context);
vm.runInContext(script, context, {filename: "sources.html"});
vm.runInContext(process.argv[2], context);
'''
    result = subprocess.run([NODE, "-e", harness, str(PAGE), assertions], text=True, capture_output=True)
    assert result.returncode == 0, result.stderr


def test_links_preserve_exact_source_version_and_node_with_reserved_characters():
    run_js(r'''
const key = "finra/2210", version = "2026-09-15:sha+a&b";
const path = sourceUrl(key, version, 42, "2210(a)(1)");
assert.ok(path.startsWith("/l1?"));
const params = new URLSearchParams(path.split("?")[1]);
assert.equal(params.get("source"), key); assert.equal(params.get("version"), version);
assert.equal(params.get("node"), "42");
location.search = path.slice(path.indexOf("?"));
const view = initialView();
assert.equal(view.version, version); assert.equal(view.nodeId, 42); assert.equal(view.ref, "2210(a)(1)");
assert.equal(sourcePath("standard/a b"), "/api/clhear/sources/standard/a%20b");
''')


def test_node_inspector_rejects_another_source_version_or_version_id():
    run_js(r'''
const doc = {version: "v2", source_version_id: 12};
const record = {source_key: "finra/2210", version_label: "v2", source_version_id: 12};
assert.equal(recordMatches(record, "finra/2210", doc), true);
assert.equal(recordMatches({...record, source_key: "finra/3110"}, "finra/2210", doc), false);
assert.equal(recordMatches({...record, version_label: "v1"}, "finra/2210", doc), false);
assert.equal(recordMatches({...record, source_version_id: 99}, "finra/2210", doc), false);
assert.equal(recordMatches({...record, version_label: undefined}, "finra/2210", doc), false);
''')


def test_structured_reader_prints_source_text_once_without_metadata_prose():
    run_js(r'''
const container = {id: 91, node_type: "article", ref: "art_1", label: "div", heading: "Subject matter", depth: 1,
  source_locator: {structure: "legal-html-element", presentation_fields: ["label", "heading"]}};
const original = DocLine({n: container, inspect: false});
assert.match(original, /node-91/);
assert.doesNotMatch(original, /Subject matter|Encoded container|class="lab"/);
assert.match(DocLine({n: container, inspect: true}), /Encoded container · art_1/);
assert.equal(tocLabel(container), "Subject matter");
for (const structure of ["xml-text", "legal-html-text", "json-value"]) {
  const text = {id: 92, node_type: "paragraph", label: "raw_field_name", raw_text: "Genuine publisher wording Ω 😀", depth: 2, source_locator: {structure}};
  const rendered = DocLine({n: text, inspect: false});
  assert.equal(rendered.split("Genuine publisher wording Ω 😀").length - 1, 1);
  assert.doesNotMatch(rendered, /raw_field_name/);
}
''')


def test_evidence_cannot_show_a_pass_from_another_version():
    run_js(r'''
const wrong = EvidencePanel({sourceKey: "finra/2210", version: "v1", card: {version: "v2", scorecard: {suites: {e1_fidelity: {passed: true}}}}});
assert.match(wrong, /No pass result is shown/);
assert.doesNotMatch(wrong, /Recorded source fidelity check passed/);
const missing = EvidencePanel({sourceKey: "finra/2210", version: "v1", card: {version: "v1", scorecard: {suites: {}}}});
assert.match(missing, /No source fidelity check recorded/);
assert.match(missing, /No completeness check recorded/);
assert.match(missing, /not completeness of the publisher/);
''')


def test_no_stored_version_is_empty_not_licensed_or_endless_loading():
    run_js(r'''
const detail = {name: "Registered standard", versions: [], changes: []};
const doc = {source: "standard/example", version: null, nodes: [], total: 0};
setStates([detail, doc]);
const view = SourceView({sourceKey: "standard/example"});
assert.match(view, /No version has been imported/);
assert.doesNotMatch(view, /Original text is withheld/);
assert.doesNotMatch(view, /Loading standard/);
setStates([detail, {...doc, version: "v1", locked: true, permission_reason: "An explicit grant is required."}]);
const restricted = SourceView({sourceKey: "standard/example"});
assert.match(restricted, /Original text is withheld/);
assert.match(restricted, /An explicit grant is required/);
''')


def test_http_access_failure_remains_an_error_and_points_to_signin():
    run_js(r'''
fetch = async () => ({ok: false, status: 401});
(async () => {
  await assert.rejects(api("/api/clhear/sources"), error => error.status === 401 && /Sign in/.test(error.message));
  assert.match(ErrorNotice({error: {status: 401, message: "Sign in"}}), /href="\/signin"/);
})().catch(error => { throw error; });
''')


def test_inspector_uses_actual_node_api_identity_and_clause_span_fields():
    run_js(r'''
const info = {id: 7, source_key: "finra/2210", source_version_id: 2, version_label: "v2", node_type: "provision", ref: "2210(a)", clauses: [{id: 6, ref: "2210(a)", source_version_id: 2, path: "2210(a)", ordering: 3, span_start: 49, span_end: 183, text_hash: "clause-hash"}]};
const inspector = Inspector({info, pinned: true});
assert.match(inspector, /finra\/2210/);
assert.match(inspector, /49 → 183/);
assert.match(inspector, /source=finra%2F2210&version=v2&node=7/);
assert.match(inspector, /clause-hash/);
assert.doesNotMatch(inspector, /source=undefined/);
''')


def test_eval_and_source_bookmarks_open_the_correct_tabs():
    run_js(r'''
location.search = "?view=evals";
assert.equal(initialView().page, "evidence");
location.search = "?view=sources";
assert.equal(initialView().page, "library");
''')


def test_inventory_missing_evidence_is_not_zero_or_full_scope_success():
    run_js(r'''
const missing = InventorySummary({report: {status: "unavailable", reason: "Migration required"}});
assert.match(missing, /Inventory evidence unavailable/);
assert.match(missing, /Migration required/);
assert.match(missing, /— verified \/ — declared/);
assert.doesNotMatch(missing, /Scope verified<\/span>/);
const partial = InventorySummary({report: {status: "gaps", verified: 5, known_expected: 8, unresolved: 3, discovery_complete: false, full_scope_verified: false, sources: []}});
assert.match(partial, /5 verified \/ 8 declared documents/);
assert.match(partial, /not a full-publisher denominator/);
const stale = InventorySummary({report: {status: "verified", audit_id: 1, verified: 8, known_expected: 8, full_scope_verified: true, current_binding_valid: false}});
assert.match(stale, /Scope not verified/);
assert.match(stale, /does not match the current source versions/);
''')


def test_source_audit_and_evals_reject_another_artifact_and_separate_freshness():
    run_js(r'''
const audit = SourceAuditEvidence({audit: {status: "verified", source_version_id: 1, content_hash: "old", publisher_checked_at: "2026-01-02", ingested_at: "2026-01-01"}, doc: {source_version_id: 1, content_hash: "new"}});
assert.match(audit, /does not verify the selected text/);
assert.match(audit, /Publisher successfully checked/);
assert.match(audit, /Imported/);
const wrong = EvidencePanel({sourceKey: "finra/2210", version: "v1", sourceVersionId: 1, contentHash: "new", card: {version: "v1", source_version_id: 1, content_hash: "old"}});
assert.match(wrong, /No pass result is shown/);
const attempted = EvidencePanel({sourceKey: "finra/2210", version: "v1", card: {version: "v1", retrieved_at: "2025-01-01", last_run: {ts: "2026-01-01", status: "rights-blocked"}, scorecard: {suites: {}}}});
assert.match(attempted, /No successful publisher check recorded/);
assert.match(attempted, /Imported 2025-01-01/);
assert.match(attempted, /last recorded attempt 2026-01-01/);
assert.doesNotMatch(attempted, /Last fetch/);
''')


def test_workflow_uses_measured_durations_and_recorded_dependencies_only():
    run_js(r'''
assert.equal(measuredDuration(null), "not recorded");
assert.equal(measuredDuration(undefined), "not recorded");
assert.equal(measuredDuration(0), "0 ms");
assert.equal(measuredDuration(1250), "1.25 s");
const missing = WorkflowEvidence({report: {status: "unavailable", reason: "No task table"}});
assert.match(missing, /Workflow evidence unavailable/);
const report = {status: "available", jobs: [{job_id: "j1", status: "running"}], tasks: [{task_id: "t1", job_id: "j1", source_key: "finra/2210", worker: "l1.finra", attempt: 2, max_attempts: 3, heartbeat_at: "2026-09-15T00:00:03Z", queue_wait_ms: 0}], steps: [{step_id: "s1", job_id: "j1", task_id: "t1", stage: "permission", attempt: 1, status: "blocked", duration_ms: 1250}, {step_id: "s2", job_id: "j1", task_id: "t1", stage: "permission", attempt: 2, status: "running", duration_ms: null, depends_on: []}]};
const workflow = WorkflowEvidence({report});
assert.match(workflow, /1.25 s/);
assert.match(workflow, /l1.finra/);
assert.match(workflow, /2026-09-15T00:00:03Z/);
assert.match(workflow, /attempt 2 \/ 3/);
assert.match(workflow, /not recorded/);
assert.match(workflow, /No dependencies/);
assert.doesNotMatch(workflow, /NaN|Animate/);
''')


def test_viewer_header_identifies_projection_origin_without_acceptance_claim():
    run_js(r'''
setStates([{viewer_snapshot:true, source_environment:"local_sqlite_test", revision:"test-revision", generated_at:"2026-09-15T00:00:00Z", omitted_layers:["L2","L3"], redacted_source_keys:["test/source"]}]);
const viewer = ViewerSnapshotNotice();
assert.match(viewer, /L1 candidate viewer/);
assert.match(viewer, /local sqlite test/);
assert.match(viewer, /test-revision/);
assert.match(viewer, /not an accepted release/);
assert.match(viewer, /1 sources have text withheld/);
''')

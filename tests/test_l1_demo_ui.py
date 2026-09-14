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

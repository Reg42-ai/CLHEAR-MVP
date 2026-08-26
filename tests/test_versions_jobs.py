"""Standardized version model + curated context + fleet job graph tests.

Fleet-wide and offline (recorded fixtures), per working rule 7.
"""
import re

import pytest
import sqlalchemy as sa

from app.clhear.l1 import families, pipeline
from app.clhear.l1.adapters import ADAPTER_KEYS, get_adapter
from app.clhear.l1.adapters.base import Artifact, DocNode, FetchResult, SourceMeta
from app.clhear.l1.adapters.eur_lex import EurLexAdapter
from app.clhear.l1.adapters.uk_legislation import UkLegislationAdapter
from app.clhear.l1.models import VERSION_KINDS, source_versions, sources

LABEL_RE = re.compile(r"^(as-published|consolidated|edition):\S+")


# ------------------------------------------------- standardized version model
@pytest.mark.parametrize("key", list(ADAPTER_KEYS))
def test_every_adapter_declares_standardized_versions(key):
    adapter = get_adapter(key)
    meta = adapter.meta()
    assert meta.about.strip(), f"{key}: SourceMeta.about must be curated"
    assert meta.topics, f"{key}: SourceMeta.topics must be curated"
    assert meta.version_policy, f"{key}: version_policy must be declared"
    result = adapter.fetch()
    assert result.version_kind in VERSION_KINDS, f"{key}: invalid kind {result.version_kind}"
    assert LABEL_RE.match(result.version_label), f"{key}: non-standard label {result.version_label}"
    prefix = result.version_label.split(":")[0]
    assert prefix == {"as_published": "as-published", "consolidated": "consolidated", "edition": "edition"}[
        result.version_kind
    ]
    if result.version_kind == "consolidated":
        assert result.as_of_date is not None, f"{key}: consolidated versions need as_of_date"


def test_uk_as_made_baseline_is_as_published_and_passes_gate():
    from app.clhear.l1 import fidelity

    adapter = UkLegislationAdapter(as_made=True)
    result = adapter.fetch()
    assert result.version_kind == "as_published"
    assert result.version_label == "as-published:2017-06-22"
    assert str(result.as_of_date) == "2017-06-22"
    report = fidelity.check(result.tree, adapter.expected_text(result.artifacts))
    assert report.ok(0.995), report.summary()


def test_gdpr_kinds_and_recitals():
    oj = EurLexAdapter(celex_version="32016R0679")
    assert oj.version_kind == "as_published"
    assert oj.version_label == "as-published:2016-05-04"
    result = oj.fetch()
    recitals = [n for root in result.tree for n in root.walk() if n.node_type == "recital"]
    assert len(recitals) == 173

    consolidated = EurLexAdapter()
    assert consolidated.version_kind == "consolidated"
    assert consolidated.version_label == "consolidated:2016-05-04"
    result_c = consolidated.fetch()
    assert not [n for root in result_c.tree for n in root.walk() if n.node_type == "recital"]


def test_meta_endpoint_serves_the_dictionary(client, engine):
    payload = client.get("/api/clhear/meta").json()
    assert set(payload["version_kinds"]) == {"as_published", "consolidated", "edition"}
    for entry in payload["version_kinds"].values():
        assert entry["label"] and len(entry["definition"]) > 50
    assert payload["fidelity_threshold"] == 0.995


def test_provenance_block_and_about(engine, client, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    pipeline.ingest(engine, UkLegislationAdapter(as_made=True), store)
    pipeline.ingest(engine, UkLegislationAdapter(snapshot="2020-01-09"), store)
    pipeline.ingest(engine, UkLegislationAdapter(), store)
    families.sync_citator(engine, UkLegislationAdapter())

    detail = client.get("/api/clhear/sources/uksi/2017/692").json()
    assert "anti-money-laundering" in detail["about"]
    assert "aml" in detail["topics"]

    states = detail["provenance"]["text_states"]
    assert [s["version_kind"] for s in states] == ["as_published", "consolidated", "consolidated"]
    assert [s["status"] for s in states] == ["superseded", "superseded", "in_force"]
    assert states[0]["as_of_date"] == "2017-06-22"

    instruments = {i["key"]: i for i in detail["provenance"]["related_instruments"]}
    assert "uksi/2019/1511" in instruments
    assert instruments["uksi/2019/1511"]["relation"] == "amends"
    assert instruments["uksi/2019/1511"]["tier"] == "binding"

    # Preamble notice data: the consolidated document points at the as-published sibling.
    doc = client.get("/api/clhear/sources/uksi/2017/692/document").json()
    assert doc["version_kind"] == "consolidated"
    assert doc["as_published_sibling"] == "as-published:2017-06-22"
    oj_doc = client.get(
        "/api/clhear/sources/uksi/2017/692/document?version_label=as-published:2017-06-22"
    ).json()
    assert oj_doc["as_published_sibling"] is None


# --------------------------------------------------------------- job graph
class _JobStub:
    key = "jobstub"

    def __init__(self, source: str, version: str, ok: bool = True):
        self.source = source
        self.version = version
        self.ok = ok

    def meta(self) -> SourceMeta:
        return SourceMeta(
            family_key="job-family",
            family_name="Job family",
            source_key=self.source,
            name=f"Job source {self.source}",
            kind="regulation",
            issuer="stub",
            jurisdiction="XX",
            license="open",
            canonical_url="https://example.invalid",
            adapter="jobstub",
            about="stub",
            topics=["stub"],
            version_policy="edition",
        )

    def fetch(self, since_version=None):
        text = f"body of {self.source} {self.version} " + "tok " * 30
        tree = [DocNode(node_type="provision", ref="p1", raw_text=text)]
        if not self.ok:
            # parser drops the artifact body entirely -> gate exhaustion
            tree = [DocNode(node_type="provision", ref="p1", raw_text="wrong")]
        return FetchResult(
            version_label=self.version,
            artifacts=[Artifact(name="doc.txt", content=(text + " missing tail " * 40).encode() if not self.ok else text.encode())],
            tree=tree,
            version_kind="edition",
        )

    def expected_text(self, artifacts):
        return artifacts[0].content.decode().split("\n")


def test_job_graph_endpoint(engine, client, tmp_path):
    store = pipeline.LocalStore(tmp_path / "lake")
    job_id = "job-test-1"
    pipeline.ingest(engine, _JobStub("job/a", "edition:1"), store, job_id=job_id)
    pipeline.ingest(engine, _JobStub("job/a", "edition:2"), store, job_id=job_id)  # chain in lane a
    pipeline.ingest(engine, _JobStub("job/b", "edition:1"), store, job_id=job_id)
    pipeline.ingest(engine, _JobStub("job/c", "edition:1", ok=False), store, job_id=job_id)  # failure node
    relay = pipeline.RunRecorder(engine, "l0.relay", "cli", {"source": "events", "job_id": job_id})
    relay.stage("relay", events=3)
    relay.finish("succeeded", {"relayed": 3})

    job = client.get("/api/clhear/jobs/latest").json()
    assert job["job_id"] == job_id
    lanes = {l["source"]: l["tasks"] for l in job["lanes"]}
    assert len(lanes["job/a"]) == 2 and len(lanes["job/b"]) == 1
    assert job["relay"]["fleet"] == "l0.relay"

    statuses = {t["source"]: t["status"] for lane in job["lanes"] for t in lane["tasks"]}
    assert statuses["job/c"] == "failure"
    assert job["status_counts"]["failure"] == 1

    # edges: start fans out to each lane head; lane a chains; all converge on relay
    a1, a2 = (t["run_id"] for t in lanes["job/a"])
    edges = {tuple(e) for e in job["edges"]}
    assert (0, a1) in edges and (a1, a2) in edges
    assert (a2, job["relay"]["run_id"]) in edges
    assert (lanes["job/b"][0]["run_id"], job["relay"]["run_id"]) in edges

    same = client.get(f"/api/clhear/jobs/{job_id}").json()
    assert same["job_id"] == job_id

    # steps carry the produced version for canvas annotations
    assert any("edition:2" in t["step"] for t in lanes["job/a"])

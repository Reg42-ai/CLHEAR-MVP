"""HLD v2 §9 — the never-list, enforced in CI.

* Inference only through Reg42 Infer on Bedrock (I6): no self-hosted model
  runtime in production infrastructure, no Chinese-origin model in a derivation
  ladder.
* Agnostic store (I5): the release scan finds organization identifiers / PII.
* No deletion (I2): covered in test_record.test_no_delete_anywhere.
"""
from pathlib import Path

from app.clhear.platform import agnostic_scan
from app.clhear.platform import task_classes as tc

REPO = Path(__file__).resolve().parents[1]
INFRA = REPO / "infra"


def _tf_text() -> str:
    return "\n".join(p.read_text(encoding="utf-8") for p in INFRA.glob("*.tf"))


def test_no_self_hosted_model_runtime_in_infrastructure():
    text = _tf_text().lower()
    for banned in ("ollama", "g6.xlarge", "gpu_orphan", "nvidia"):
        assert banned not in text, f"{banned!r} still referenced in infra/*.tf (I6: Bedrock via Infer only)"
    assert not (INFRA / "gpu.tf").exists()


def test_derivation_ladders_are_procurement_clean():
    for name in tc.DERIVATION_CLASSES:
        for rung in tc.default_ladder(name):
            assert tc.is_procurement_clean(rung), f"{name}: {rung}"
    assert not tc.is_procurement_clean(tc.QWEN_3_5_32B)
    assert not tc.is_procurement_clean(tc.DEEPSEEK_R1)
    assert tc.validate_ladders({"l2_extract": [tc.QWEN_3_5_32B]})


def test_infer_task_yaml_covers_every_required_class():
    text = tc.to_infer_yaml()
    for name in tc.REQUIRED_TASK_CLASSES:
        assert f"{name}:" in text


def test_agnostic_scan_flags_pii_and_org_identifiers(tmp_path):
    (tmp_path / "manifest.json").write_text('{"id": "2026.09.28"}')
    (tmp_path / "leak.txt").write_text(
        "contact jane.doe@acme-bank.example.net; IBAN GB82 WEST 1234 5698 7654 32; Acme Bank plc"
    )
    report = agnostic_scan.scan_path(tmp_path, denylist=["acme bank"])
    kinds = {h.kind for h in report.hits}
    assert {"email", "iban", "organization_identifier"} <= kinds
    assert not report.clean


def test_agnostic_scan_allows_publisher_addresses(tmp_path):
    (tmp_path / "corpus.txt").write_text("Enquiries: enquiries@fca.org.uk. Account 730649732189 hosts the store.")
    report = agnostic_scan.scan_path(tmp_path, denylist=[])
    assert report.clean, [h.__dict__ for h in report.hits]


def test_agnostic_scan_of_store_is_clean(engine):
    report = agnostic_scan.scan_engine(engine, denylist=[])
    assert report.clean, [h.__dict__ for h in report.hits][:5]

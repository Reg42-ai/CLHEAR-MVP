"""HLD v2 §9 — the never-list, enforced in CI.

* Inference only through Reg42 Infer on Bedrock (I6): no self-hosted model
  runtime in production infrastructure, no Chinese-origin model in a derivation
  ladder.
* Agnostic store (I5): the release scan finds organization identifiers / PII.
* No deletion (I2): covered in test_record.test_no_delete_anywhere.
"""
from pathlib import Path

import pytest

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


def test_only_infer_provider_in_prod(monkeypatch):
    """§9: no inference outside Reg42 Infer. Production settings can only yield
    the Infer provider; the gateway module defines no other real provider; and
    no module outside the gateway talks HTTP to a model endpoint."""
    import inspect

    from app.clhear.platform import gateway
    from app.clhear.platform.router import build_providers
    from app.clhear.settings import get_settings

    monkeypatch.setenv("CLHEAR_LLM_PROVIDER", "")
    monkeypatch.setenv("INFER_BASE_URL", "https://infer.reg42.ai/v1")
    monkeypatch.setenv("INFER_TOKEN", "tok")
    get_settings.cache_clear()
    try:
        assert set(build_providers()) == {"infer"}
    finally:
        get_settings.cache_clear()
    real = [
        n for n, obj in inspect.getmembers(gateway, inspect.isclass)
        if n.endswith("Provider") and obj.__module__ == gateway.__name__ and n not in ("Provider", "FakeProvider")
    ]
    assert real == ["InferProvider"]
    vendor_hosts = ("api.anthropic.com", "api.openai.com", "api.x.ai", "ollama.com", "generativelanguage.googleapis.com", "bedrock-runtime")
    # One documented exemption: clause embeddings (a rebuildable projection, never
    # record content) may call Titan/Cohere embed models on Bedrock directly while
    # the pinned Infer image has no /embeddings route. IAM pins the callable models.
    embed_exempt = REPO / "app" / "clhear" / "platform" / "embeddings.py"
    offenders = []
    for path in (REPO / "app").rglob("*.py"):
        text = path.read_text(encoding="utf-8")
        for host in vendor_hosts:
            if host in text and not (path == embed_exempt and host == "bedrock-runtime"):
                offenders.append(f"{path.relative_to(REPO)}: {host}")
    assert offenders == []
    iam = (REPO / "infra" / "iam.tf").read_text(encoding="utf-8")
    embed_stmt = iam[iam.index('Sid      = "EmbeddingModels"'):]
    assert "local.embedding_model_arns" in embed_stmt.split("},")[0]
    arns = iam[iam.index("embedding_model_arns = ["):].split("]")[0]
    assert "foundation-model/amazon.titan-embed-text-v2:0" in arns and "foundation-model/cohere.embed-multilingual-v3" in arns
    assert not any(bad in arns for bad in ("anthropic.", "mistral.", "openai.", "meta.", "nova", "*"))


def test_no_gateway_calls_outside_router():
    """§9: every LLM call goes through router.run — nothing else constructs a
    Gateway call or invokes a provider's complete() directly."""
    import re

    pattern = re.compile(r"gateway\.call\(|\.complete\(\s*\n?\s*\*?\s*model=|Gateway\(")
    allowed = {"app/clhear/platform/router.py", "app/clhear/platform/gateway.py"}
    offenders = []
    for path in (REPO / "app").rglob("*.py"):
        rel = str(path.relative_to(REPO))
        if rel in allowed:
            continue
        for lineno, line in enumerate(path.read_text(encoding="utf-8").splitlines(), 1):
            if pattern.search(line) and "Gateway | None" not in line and "-> " not in line:
                offenders.append(f"{rel}:{lineno}: {line.strip()}")
    assert offenders == []


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


def test_disclosure_gate(engine, monkeypatch):
    """§9: nothing reaches the public `clhear` repo before the filing is confirmed.
    The exporter compiles locally regardless, refuses to push without the flag,
    and the release workflow wires the flag from a repository variable."""
    from app.clhear.platform import exporter
    from app.clhear.settings import get_settings

    monkeypatch.delenv("CLHEAR_PUBLIC_DISCLOSURE_CONFIRMED", raising=False)
    monkeypatch.setenv("CLHEAR_PUBLIC_REPO_URL", "https://github.com/example/clhear.git")
    get_settings.cache_clear()
    try:
        assert exporter.disclosure_confirmed() is False
        monkeypatch.setattr(exporter, "compile_snapshot", lambda *_a, **_k: {"release": "x"})
        monkeypatch.setattr("app.clhear.platform.evals.release_gate", lambda *_a, **_k: True)
        with pytest.raises(exporter.DisclosureNotConfirmed):
            exporter.export_release(engine, "x", push=True, layout=False)
        monkeypatch.setenv("CLHEAR_PUBLIC_DISCLOSURE_CONFIRMED", "true")
        get_settings.cache_clear()
        assert exporter.disclosure_confirmed() is True
    finally:
        get_settings.cache_clear()
    workflow = (REPO / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "CLHEAR_PUBLIC_DISCLOSURE_CONFIRMED" in workflow
    # no workflow pushes to a public remote by hand — only the gated exporter does
    for wf in (REPO / ".github" / "workflows").glob("*.yml"):
        text = wf.read_text(encoding="utf-8")
        assert "git push" not in text.replace("git push --tags", ""), wf.name


def test_no_offense_defense_schema_terms(client):
    """§9 / §4.5: offense/defense is a narrative metaphor and never a schema, vocabulary
    or API term. Table and column names, side/kind enumerations, the curated catalog's
    keys and values, the GraphQL SDL, the JSON-LD context and every OpenAPI path and
    schema name are checked; regulatory *text* about criminal offences is not schema."""
    import json
    import re

    import sqlalchemy as sa

    from app.clhear.interop import graphql_api, jsonld
    from app.clhear.l5.models import FORBIDDEN_SCHEMA_TERMS, SIDES
    from app.clhear.models import metadata
    from app.clhear.platform import record

    pattern = re.compile(r"\b(?:" + "|".join(FORBIDDEN_SCHEMA_TERMS) + r")(?:s|ive|ively)?\b", re.I)
    offenders: list[str] = []

    def check(label: str, value):
        if isinstance(value, str) and pattern.search(value):
            offenders.append(f"{label}: {value!r}")

    for table in list(metadata.tables.values()) + record.layer_tables():
        check(f"table {table.fullname}", table.name)
        for col in table.columns:
            check(f"column {table.fullname}.{col.name}", col.name)
        for c in table.constraints:
            if isinstance(c, sa.CheckConstraint):
                check(f"check {table.fullname}", str(c.sqltext))
    for side in SIDES:
        check("side", side)
    curated_dir = REPO / "app" / "clhear" / "curated"
    for path in curated_dir.glob("*.json"):
        def walk(node, where):
            if isinstance(node, dict):
                for k, v in node.items():
                    check(f"{where}.{k} (key)", k)
                    if k in ("side", "kind", "action_type", "status", "type", "key", "id"):
                        check(f"{where}.{k}", v if isinstance(v, str) else "")
                    walk(v, f"{where}.{k}")
            elif isinstance(node, list):
                for i, v in enumerate(node):
                    walk(v, f"{where}[{i}]")
        walk(json.loads(path.read_text()), path.name)
    for line in graphql_api.sdl().splitlines():
        if not line.strip().startswith('"'):  # doc strings may describe the metaphor; names may not carry it
            check("graphql sdl", line)
    for term in jsonld.context_document()["@context"]:
        check("jsonld term", term)
    spec = client.get("/openapi.json").json()
    for p in spec["paths"]:
        check("openapi path", p)
    for name in (spec.get("components") or {}).get("schemas", {}):
        check("openapi schema", name)
    for path in (REPO / "export" / "clhear").rglob("*.schema.json"):
        def walk_schema(node, where):
            if isinstance(node, dict):
                for k, v in node.items():
                    if k == "properties" and isinstance(v, dict):
                        for prop in v:
                            check(f"{where}.{prop}", prop)
                    walk_schema(v, where)
            elif isinstance(node, list):
                for v in node:
                    walk_schema(v, where)
        walk_schema(json.loads(path.read_text()), path.name)
    assert not offenders, offenders

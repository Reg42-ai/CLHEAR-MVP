"""A stale viewer must not reveal cached headings after display permission expires."""
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient
import pytest
import sqlalchemy as sa

from app.clhear import accounts
from app.clhear.l1 import permissions
from app.clhear.l1.models import clauses, doc_nodes
from app.clhear.settings import get_settings
from tests.test_l1_viewer_snapshot import corpus, KEY, MARKER


@pytest.mark.parametrize("surface", ["document", "clauses", "node"])
def test_expired_permission_hides_cached_heading_label_and_path(engine, monkeypatch, surface):
    corpus(engine)
    with engine.begin() as conn:
        conn.execute(doc_nodes.update().values(label=MARKER + "-label"))
        conn.execute(clauses.update().values(path=MARKER + "-path"))
        node_id = conn.execute(sa.select(doc_nodes.c.id)).scalar_one()
    permissions.record_permission(engine, source_key=KEY,
        permissions={"store": True, "display_internal": True}, evidence_ref="test-only:expired-display",
        approved_by="test-reviewer", approved=True,
        valid_from=datetime.now(timezone.utc) - timedelta(days=2),
        expires_at=datetime.now(timezone.utc) - timedelta(days=1))
    monkeypatch.setenv("CLHEAR_RESTRICTED_ACCESS", "true")
    monkeypatch.setenv("CLHEAR_SESSION_SECRET", "test-only-reviewer-secret-longer-than-thirty-two")
    monkeypatch.setenv("CLHEAR_REVIEWER_EMAILS", "reviewer@example.test")
    get_settings.cache_clear()
    from app.main import create_app
    with TestClient(create_app(), base_url="https://testserver") as client:
        client.cookies.set(accounts.SESSION_COOKIE, accounts.session_token({
            "id": "test-reviewer", "email": "reviewer@example.test", "display_name": "Reviewer"}))
        path = f"/api/clhear/nodes/{node_id}" if surface == "node" else f"/api/clhear/sources/{KEY}/{surface}"
        response = client.get(path)
        assert response.status_code == 200
        assert response.json()["locked"] is True
        assert MARKER not in response.text
        assert "text_hash" in response.text

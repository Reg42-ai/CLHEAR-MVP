import pytest

from app.clhear import db as clhear_db
from app.clhear.db import make_engine, run_migrations
from app.clhear.settings import get_settings


@pytest.fixture()
def engine(tmp_path, monkeypatch):
    """Fresh sqlite DB per test, wired into settings + the app-level engine."""
    monkeypatch.setenv("DATABASE_URL", f"sqlite:///{tmp_path}/clhear-test.db")
    monkeypatch.setenv("CLHEAR_ARTIFACTS_DIR", str(tmp_path / "artifacts"))
    monkeypatch.setenv("CLHEAR_PUBLIC_REPO_DIR", str(tmp_path / "clhear-public"))
    monkeypatch.setenv("CLHEAR_MAINTAINERS", "avner@reg42.ai,maintainer@reg42.ai")
    get_settings.cache_clear()
    engine = make_engine(get_settings().database_url)
    run_migrations(engine)
    clhear_db.set_engine(engine)
    yield engine
    clhear_db.set_engine(None)  # type: ignore[arg-type]
    get_settings.cache_clear()


@pytest.fixture()
def client(engine):
    from fastapi.testclient import TestClient

    from app.main import create_app

    with TestClient(create_app()) as test_client:
        yield test_client

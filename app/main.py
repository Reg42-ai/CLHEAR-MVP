"""CLHEAR web service. Startup migrations, feature-flagged routes.

# ARCH: in reg42-os this router is included by the existing web service and
# served under the clhear.reg42.ai host rule; standalone here for the MVP repo.
"""
import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI

from app.clhear.db import get_engine, run_migrations
from app.clhear.settings import get_settings

logging.basicConfig(level=logging.INFO)


@asynccontextmanager
async def lifespan(app: FastAPI):
    engine = get_engine()
    run_migrations(engine)
    try:
        from app.clhear import curated

        curated.seed(engine)  # idempotent: L3/L5/L4 catalog present everywhere
    except Exception:
        logging.getLogger("clhear").exception("curated seed failed (continuing)")
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="CLHEAR", lifespan=lifespan)
    if get_settings().reg42_clhear_enabled:
        from app.clhear.accounts import router as auth_router
        from app.clhear.app_api import router as app_api_router
        from app.clhear.community import router as community_router
        from app.clhear.l1.routes import router as l1_router
        from app.clhear.layer_routes import router as layers_router
        from app.clhear.routes import router

        app.include_router(router)
        app.include_router(l1_router)
        app.include_router(app_api_router)
        app.include_router(auth_router)
        app.include_router(community_router)
        app.include_router(layers_router)  # serves "/" — the Stack UI

    return app


app = create_app()

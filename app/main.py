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
    run_migrations(get_engine())
    yield


def create_app() -> FastAPI:
    app = FastAPI(title="CLHEAR", lifespan=lifespan)
    if get_settings().reg42_clhear_enabled:
        from fastapi.responses import RedirectResponse

        from app.clhear.app_api import router as app_api_router
        from app.clhear.l1.routes import router as l1_router
        from app.clhear.routes import router

        app.include_router(router)
        app.include_router(l1_router)
        app.include_router(app_api_router)

        @app.get("/", include_in_schema=False)
        def index() -> RedirectResponse:
            return RedirectResponse("/sources")

    return app


app = create_app()

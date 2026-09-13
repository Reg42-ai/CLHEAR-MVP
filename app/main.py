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
        from app.clhear.ai_routes import router as ai_router
        from app.clhear.layer_routes import router as layers_router
        from app.clhear.routes import router
        from app.clhear.v1.l1 import router as v1_l1_router
        from app.clhear.v1.l2 import router as v1_l2_router
        from app.clhear.v1.l3 import router as v1_l3_router
        from app.clhear.v1.l4 import router as v1_l4_router
        from app.clhear.v1.l5 import router as v1_l5_router
        from app.clhear.v1.l6 import router as v1_l6_router
        from app.clhear.v1.l7 import router as v1_l7_router
        from app.clhear.v1.l8 import router as v1_l8_router
        from app.clhear.v1.solon import router as solon_router
        from app.clhear.v1.explore import router as explore_router
        from app.clhear.learn import router as learn_router
        from app.clhear.watch import router as watch_router
        from app.clhear.api_keys import router as build_router
        from app.clhear.v1.graph import router as graph_router
        from app.clhear.v1.console import router as console_router
        from app.clhear.v1.contributions import router as contributions_router
        from app.clhear.v1.conformance import router as conformance_router
        from app.clhear.v1.interop import router as interop_router

        app.include_router(router)
        app.include_router(l1_router)
        app.include_router(v1_l1_router)
        app.include_router(v1_l2_router)
        app.include_router(v1_l3_router)
        app.include_router(v1_l4_router)
        app.include_router(v1_l5_router)
        app.include_router(v1_l6_router)
        app.include_router(v1_l7_router)
        app.include_router(v1_l8_router)  # fills + member benchmarks; members-only content, public metadata
        app.include_router(graph_router)
        app.include_router(console_router)
        app.include_router(contributions_router)  # /contribute, /contributions, /cla, /governance
        app.include_router(conformance_router)  # Annex E self-assessments, marks register, assessors
        app.include_router(interop_router)  # crosswalks, JSON-LD, GraphQL
        app.include_router(app_api_router)
        app.include_router(auth_router)
        app.include_router(community_router)
        app.include_router(ai_router)
        app.include_router(solon_router)  # serves "/" — the Solon front door
        app.include_router(explore_router)
        app.include_router(learn_router)
        app.include_router(watch_router)
        app.include_router(build_router)
        app.include_router(layers_router)  # serves "/stack" — the Stack UI

    return app


app = create_app()

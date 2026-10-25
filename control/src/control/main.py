"""FastAPI application shell.

Liveness for the node start/stop scripts: GET /healthz must answer 200 without
touching the database (k3s pulls it after barrier-ready; done before Postgres is
even reachable). Readiness: GET /api/v1/healthz reports DB connectivity truthfully.

Routes live under `control/routers/<area>.py`. This module owns composition only:
middleware, lifespan, router registration and the two health endpoints.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware

from control import db as db_module
from control.errors import RequestContextMiddleware, install_error_handlers
from control.routers import auth, deployments, domains, incidents, orgs, projects, secrets
from control.settings import get_settings


def create_app() -> FastAPI:
    settings = get_settings()
    db = db_module.Db()

    @asynccontextmanager
    async def lifespan(_: FastAPI):
        await db.connect(settings)
        try:
            yield
        finally:
            await db.close()

    app = FastAPI(
        title="AutoPilot control plane",
        version="0.1.0",
        lifespan=lifespan,
        openapi_url="/api/v1/openapi.json",
        docs_url="/api/v1/docs",
        redoc_url=None,
    )
    app.state.db = db
    app.state.settings = settings

    # The request id/trace and tenant-field rejection apply to every route,
    # including errors. CORS is outermost so a browser can read a problem+json
    # response instead of seeing an opaque network error.
    app.add_middleware(RequestContextMiddleware)
    # Only the configured console origin is trusted in a deployment. The Vite
    # and local-ingress origins are a development convenience and must not widen
    # the production CORS policy.
    allowed_origins = [settings.console_base_url.rstrip("/")]
    if settings.env_name == "dev":
        allowed_origins += [
            "http://localhost:5173",
            "http://localhost:4173",
            "http://console.apps.localtest.me:8080",
        ]
    app.add_middleware(
        CORSMiddleware,
        allow_origins=allowed_origins,
        allow_credentials=True,
        allow_methods=["*"],
        allow_headers=["*"],
        expose_headers=["X-Request-Id", "traceparent"],
    )
    install_error_handlers(app)

    prefix = "/api/v1"
    app.include_router(auth.me_router, prefix=prefix)
    app.include_router(auth.router, prefix=prefix)
    app.include_router(orgs.router, prefix=prefix)
    app.include_router(projects.router, prefix=prefix)
    app.include_router(deployments.router, prefix=prefix)
    app.include_router(secrets.router, prefix=prefix)
    app.include_router(domains.router, prefix=prefix)
    app.include_router(incidents.router, prefix=prefix)

    @app.get("/healthz")
    async def healthz() -> dict:
        # no DB touch: the node boot barrier checks this endpoint
        return {"status": "ok", "env": settings.env_name}

    @app.get("/api/v1/healthz")
    async def api_healthz() -> dict:
        ok = False
        if db.pool is not None:
            try:
                await db.pool.fetchval("SELECT 1")
                ok = True
            except Exception:  # noqa: BLE001 - readiness reports truth, not errors
                ok = False
        return {"status": "ok", "db": ok}

    return app


app = create_app()

"""FastAPI application shell.

Liveness for the node start/stop scripts: GET /healthz must answer 200 without
touching the database (k3s pulls it after barrier-ready; done before Postgres is
even reachable). Readiness: GET /api/v1/healthz reports DB connectivity truthfully.

SPEC-04 (API surface, auth, pagination) extends this shell - routes belong under
/app/routers/<spec>.py when that work starts. Keep this file structural.
"""

from __future__ import annotations

from contextlib import asynccontextmanager

from fastapi import FastAPI

from control import db as db_module
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
    )
    app.state.db = db
    app.state.settings = settings

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

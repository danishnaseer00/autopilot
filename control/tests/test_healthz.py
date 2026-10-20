"""Shell tests: the app boots with no configuration at all (worker machines run
pytest before any container is up), /healthz never touches the DB."""

from __future__ import annotations

from fastapi.testclient import TestClient

from control.main import create_app


def test_healthz_liveness():
    with TestClient(create_app()) as client:
        response = client.get("/healthz")
        assert response.status_code == 200
        body = response.json()
        assert body["status"] == "ok"
        assert "env" in body


def test_api_healthz_reports_db_truthfully(monkeypatch):
    # force an empty DSN even when the caller's environment has one set:
    # this test proves the shell reports db=False when no database exists
    monkeypatch.setenv("APP_DATABASE_URL", "")
    monkeypatch.setenv("AUDIT_DATABASE_URL", "")
    from control.settings import get_settings
    get_settings.cache_clear()
    try:
        with TestClient(create_app()) as client:
            response = client.get("/api/v1/healthz")
            assert response.status_code == 200
            body = response.json()
            assert body["status"] == "ok"
            assert body["db"] is False
    finally:
        get_settings.cache_clear()

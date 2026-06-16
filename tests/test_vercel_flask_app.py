from __future__ import annotations

from app import app


def test_root_flask_app_exports_vercel_app():
    assert app.import_name == "app"


def test_flask_health_route_is_available():
    client = app.test_client()
    response = client.get("/api/health")

    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert response.get_json()["runtime"] == "vercel-python-flask"


def test_flask_cron_requires_secret_when_configured(monkeypatch):
    monkeypatch.setenv("VERCEL_CRON_SECRET", "secret")
    client = app.test_client()
    response = client.get("/api/cron")

    assert response.status_code == 401
    assert response.get_json()["error"] == "unauthorized"

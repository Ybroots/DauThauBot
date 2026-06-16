from __future__ import annotations

import json

from api.index import app


def _call(path: str, *, method: str = "GET", query: str = "", headers: dict | None = None):
    captured: dict = {}
    environ = {
        "PATH_INFO": path,
        "REQUEST_METHOD": method,
        "QUERY_STRING": query,
    }
    for key, value in (headers or {}).items():
        environ["HTTP_" + key.upper().replace("-", "_")] = value

    def start_response(status, response_headers):
        captured["status"] = status
        captured["headers"] = response_headers

    body = b"".join(app(environ, start_response))
    captured["json"] = json.loads(body.decode("utf-8"))
    return captured


def test_health_route_is_available():
    response = _call("/api/health")

    assert response["status"] == "200 OK"
    assert response["json"]["ok"] is True
    assert response["json"]["runtime"] == "vercel-python-wsgi"


def test_unknown_route_returns_404():
    response = _call("/api/missing")

    assert response["status"] == "404 Not Found"
    assert response["json"]["ok"] is False


def test_cron_requires_secret_when_configured(monkeypatch):
    monkeypatch.setenv("VERCEL_CRON_SECRET", "secret")
    response = _call("/api/cron")

    assert response["status"] == "401 Unauthorized"
    assert response["json"]["error"] == "unauthorized"

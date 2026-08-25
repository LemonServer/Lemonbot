from __future__ import annotations

from fastapi.testclient import TestClient

from lemonbot.admin.app import create_admin_app
from lemonbot.admin.auth import LocalTokenManager
from lemonbot.admin.control import InMemoryControl


def test_one_time_login_cookie_and_csrf() -> None:
    tokens = LocalTokenManager()
    token = tokens.issue_bootstrap()
    app = create_admin_app(InMemoryControl("lab", "fake"), tokens, port=8765)
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        exchange = client.post(
            "/auth/exchange",
            json={"token": token},
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        assert exchange.status_code == 200
        assert "HttpOnly" in exchange.headers["set-cookie"]
        csrf = exchange.json()["csrf"]
        assert client.get("/api/status").status_code == 200
        assert (
            client.post(
                "/api/pause/global",
                json={"paused": True},
                headers={"Origin": "http://127.0.0.1:8765"},
            ).status_code
            == 403
        )
        paused = client.post(
            "/api/pause/global",
            json={"paused": True},
            headers={
                "Origin": "http://127.0.0.1:8765",
                "X-CSRF-Token": csrf,
            },
        )
        assert paused.status_code == 200
        assert paused.json()["global_paused"] is True
        replay = client.post(
            "/auth/exchange",
            json={"token": token},
            headers={"Origin": "http://127.0.0.1:8765"},
        )
        assert replay.status_code == 401


def test_admin_rejects_host_and_cross_origin() -> None:
    tokens = LocalTokenManager()
    token = tokens.issue_bootstrap()
    app = create_admin_app(InMemoryControl("lab", "fake"), tokens, port=8765)
    with TestClient(app, base_url="http://evil.example") as client:
        assert client.get("/healthz").status_code == 400
    with TestClient(app, base_url="http://127.0.0.1:8765") as client:
        response = client.post(
            "/auth/exchange",
            json={"token": token},
            headers={"Origin": "https://evil.example"},
        )
        assert response.status_code == 403

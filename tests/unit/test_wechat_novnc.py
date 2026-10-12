from __future__ import annotations

import importlib.util
from pathlib import Path
from unittest.mock import AsyncMock

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

_spec = importlib.util.spec_from_file_location(
    "wechat_novnc", Path(__file__).parents[2] / "scripts/wechat_novnc.py"
)
assert _spec and _spec.loader
novnc = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(novnc)


@pytest.mark.parametrize(
    ("token", "origin"),
    [
        ("", novnc.ORIGIN),
        ("b" * 64, novnc.ORIGIN),
        ("非认证令牌", novnc.ORIGIN),
        ("a" * 64, "http://external.example"),
        ("a" * 64, ""),
    ],
)
def test_novnc_rejects_unauthenticated_or_cross_origin_before_vnc_connection(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch, token: str, origin: str
) -> None:
    tcp = AsyncMock()
    monkeypatch.setattr(novnc.asyncio, "open_connection", tcp)
    app = novnc.create_app("172.17.0.2", "a" * 64, tmp_path)
    with TestClient(app) as client:
        with pytest.raises(WebSocketDisconnect) as error:
            with client.websocket_connect(
                "/websockify", params={"token": token}, headers={"origin": origin}
            ):
                pytest.fail("unauthorized connection accepted")
    assert error.value.code == 1008
    tcp.assert_not_awaited()


def test_novnc_serves_assets_and_forwards_only_authenticated_binary_vnc(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    (tmp_path / "fixture.js").write_text("synthetic frontend")
    reader = AsyncMock()
    reader.read.side_effect = [b"RFB 003.008\n", b""]
    writer = AsyncMock()
    writer.close = lambda: None
    tcp = AsyncMock(return_value=(reader, writer))
    monkeypatch.setattr(novnc.asyncio, "open_connection", tcp)
    app = novnc.create_app("172.17.0.2", "a" * 64, tmp_path)
    with TestClient(app) as client:
        assert client.get("/").status_code == 200
        assert client.get("/assets/fixture.js").text == "synthetic frontend"
        assert client.get("/api/messages/send").status_code == 404
        with client.websocket_connect(
            "/websockify?token=" + "a" * 64,
            headers={"origin": novnc.ORIGIN},
            subprotocols=["binary"],
        ) as socket:
            assert socket.receive_bytes() == b"RFB 003.008\n"
    tcp.assert_awaited_once_with("172.17.0.2", 5900)
    writer.wait_closed.assert_awaited_once()

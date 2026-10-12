#!/usr/bin/env python3
"""Authenticated loopback noVNC for the ARM64 image's existing VNC server.

Static third-party assets are installed separately under the desktop user's share
directory. No API/send proxy, account data or keys are exposed by this service.
"""

from __future__ import annotations

import argparse
import asyncio
import hmac
import ipaddress
from pathlib import Path

import uvicorn
from fastapi import FastAPI, WebSocket, WebSocketDisconnect
from fastapi.responses import HTMLResponse
from fastapi.staticfiles import StaticFiles

from lemonbot.connectors.wechat_agent_state import private_read

PORT = 6176
ORIGIN = f"http://127.0.0.1:{PORT}"
HTML = """<!doctype html><html lang="zh-CN"><meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1">
<title>Lemonbot 微信桌面</title>
<style>body{margin:0;background:#202428;color:#fff;font:16px sans-serif}
header{padding:12px}button{margin-left:16px}#screen{height:calc(100vh - 50px)}</style>
<header>微信桌面 · <span id="status">正在连接</span>
<button id="connect">重新连接</button></header><div id="screen"></div>
<script type="module">
import RFB from '/assets/core/rfb.js';
const token = new URLSearchParams(location.hash.slice(1)).get('token');
history.replaceState(null, '', '/');
const status = document.getElementById('status');
let rfb;
function connect() {
  if (!token) {status.textContent = '请通过本机部署脚本打开认证页面'; return;}
  if (rfb) rfb.disconnect();
  status.textContent = '正在连接';
  rfb = new RFB(document.getElementById('screen'),
    'ws://' + location.host + '/websockify?token=' + encodeURIComponent(token));
  rfb.viewOnly = false;
  rfb.scaleViewport = true;
  rfb.addEventListener('connect', () => {status.textContent = '已连接 · 请手动扫码登录';});
  rfb.addEventListener('disconnect', () => {status.textContent = '连接已断开';});
  rfb.addEventListener('securityfailure', () => {status.textContent = '认证失败，请重新打开页面';});
}
document.getElementById('connect').onclick = connect;
connect();
</script></html>"""


def create_app(forward_ip: str, token: str, assets: Path) -> FastAPI:
    address = ipaddress.ip_address(forward_ip)
    if address.version != 4 or not address.is_private or address.is_unspecified:
        raise ValueError("PrivateContainerAddressRequired")
    if len(token) != 64 or any(c not in "0123456789abcdef" for c in token):
        raise ValueError("InvalidPrivateToken")
    app = FastAPI(docs_url=None, redoc_url=None, openapi_url=None)
    app.mount("/assets", StaticFiles(directory=assets), name="assets")

    @app.get("/")
    async def index() -> HTMLResponse:
        return HTMLResponse(HTML, headers={"Cache-Control": "no-store"})

    @app.get("/health")
    async def health() -> dict[str, bool]:
        return {"ok": True}

    @app.websocket("/websockify")
    async def websocket(websocket: WebSocket) -> None:
        if (
            websocket.headers.get("origin") != ORIGIN
            or not hmac.compare_digest(
                websocket.query_params.get("token", "").encode(), token.encode()
            )
        ):
            await websocket.close(code=1008)
            return
        try:
            reader, writer = await asyncio.wait_for(
                asyncio.open_connection(str(address), 5900), timeout=10
            )
        except (OSError, TimeoutError):
            await websocket.close(code=1011)
            return
        tasks: list[asyncio.Task[None]] = []
        try:
            protocols = websocket.scope.get("subprotocols", [])
            await websocket.accept(subprotocol="binary" if "binary" in protocols else None)

            async def to_vnc() -> None:
                while True:
                    data = await websocket.receive_bytes()
                    writer.write(data)
                    await writer.drain()

            async def to_browser() -> None:
                while data := await reader.read(65536):
                    await websocket.send_bytes(data)

            tasks = [asyncio.create_task(to_vnc()), asyncio.create_task(to_browser())]
            done, _pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
            for task in done:
                task.result()
        except (OSError, WebSocketDisconnect, RuntimeError):
            pass
        finally:
            for task in tasks:
                task.cancel()
            await asyncio.gather(*tasks, return_exceptions=True)
            writer.close()
            await writer.wait_closed()

    return app


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--forward-ip", required=True)
    parser.add_argument("--token-file", type=Path, required=True)
    parser.add_argument("--assets", type=Path, required=True)
    args = parser.parse_args()
    token = private_read(args.token_file).decode().strip()
    app = create_app(args.forward_ip, token, args.assets)
    # WebSocket URLs contain authentication. Disable request and protocol logging.
    uvicorn.run(
        app, host="127.0.0.1", port=PORT, ws="websockets", access_log=False, log_level="critical"
    )


if __name__ == "__main__":
    main()

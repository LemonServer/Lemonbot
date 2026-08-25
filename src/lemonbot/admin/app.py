# ruff: noqa: E501

from __future__ import annotations

import hmac
from typing import Annotated, Literal

from fastapi import Cookie, FastAPI, Header, HTTPException, Request, Response
from fastapi.responses import HTMLResponse
from pydantic import BaseModel, ConfigDict
from starlette.middleware.base import BaseHTTPMiddleware, RequestResponseEndpoint
from starlette.types import ASGIApp

from lemonbot.admin.auth import AuthenticationError, LocalTokenManager, Session
from lemonbot.admin.control import ControlBackend


class ExchangeRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    token: str


class PauseRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    paused: bool


class ApprovalRequest(BaseModel):
    model_config = ConfigDict(extra="forbid")
    decision: Literal["approve_once", "deny"]


class LoopbackGuardMiddleware(BaseHTTPMiddleware):
    def __init__(self, app: ASGIApp, *, host: str, port: int) -> None:
        super().__init__(app)
        authority = f"[{host}]" if ":" in host else host
        self._allowed_hosts = {
            authority,
            f"{authority}:{port}",
            "localhost",
            f"localhost:{port}",
        }
        self._allowed_origins = {
            f"http://{authority}:{port}",
            f"http://localhost:{port}",
        }

    async def dispatch(self, request: Request, call_next: RequestResponseEndpoint) -> Response:
        host = request.headers.get("host", "").lower()
        if host not in self._allowed_hosts:
            return Response("invalid host", status_code=400)
        if request.method not in {"GET", "HEAD", "OPTIONS"}:
            origin = request.headers.get("origin")
            if origin not in self._allowed_origins:
                return Response("invalid origin", status_code=403)
        response = await call_next(request)
        response.headers.setdefault("Cache-Control", "no-store")
        response.headers.setdefault("X-Content-Type-Options", "nosniff")
        response.headers.setdefault("X-Frame-Options", "DENY")
        response.headers.setdefault("Referrer-Policy", "no-referrer")
        response.headers.setdefault(
            "Content-Security-Policy",
            "default-src 'self'; frame-ancestors 'none'; base-uri 'none'; "
            "form-action 'self'; object-src 'none'; style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'",
        )
        return response


def create_admin_app(
    control: ControlBackend,
    tokens: LocalTokenManager,
    *,
    host: str = "127.0.0.1",
    port: int = 8765,
) -> FastAPI:
    app = FastAPI(
        title="Lemonbot Local Admin",
        docs_url=None,
        redoc_url=None,
        openapi_url=None,
    )
    app.add_middleware(LoopbackGuardMiddleware, host=host, port=port)

    def require_session(cookie: str | None) -> Session:
        try:
            return tokens.authenticate(cookie)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc

    def require_csrf(session: Session, csrf: str | None) -> None:
        if not csrf or not hmac.compare_digest(session.csrf, csrf):
            raise HTTPException(status_code=403, detail="invalid CSRF token")

    @app.get("/healthz")
    async def healthz() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/login", response_class=HTMLResponse)
    async def login() -> str:
        return _LOGIN_HTML

    @app.post("/auth/exchange")
    async def exchange(payload: ExchangeRequest, response: Response) -> dict[str, str]:
        try:
            session = tokens.exchange(payload.token)
        except AuthenticationError as exc:
            raise HTTPException(status_code=401, detail=str(exc)) from exc
        response.set_cookie(
            "lemonbot_session",
            session.token,
            httponly=True,
            samesite="strict",
            secure=False,
            max_age=8 * 60 * 60,
            path="/",
        )
        response.headers["Cache-Control"] = "no-store"
        return {"csrf": session.csrf}

    @app.post("/auth/logout")
    async def logout(
        response: Response,
        lemonbot_session: Annotated[str | None, Cookie()] = None,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, bool]:
        session = require_session(lemonbot_session)
        require_csrf(session, x_csrf_token)
        tokens.revoke(lemonbot_session)
        response.delete_cookie("lemonbot_session", path="/")
        return {"ok": True}

    @app.get("/api/status")
    async def status(
        lemonbot_session: Annotated[str | None, Cookie()] = None,
    ) -> dict[str, object]:
        require_session(lemonbot_session)
        return (await control.status()).model_dump(mode="json")

    @app.get("/api/approvals")
    async def approvals(
        lemonbot_session: Annotated[str | None, Cookie()] = None,
    ) -> list[dict[str, object]]:
        require_session(lemonbot_session)
        return [item.model_dump(mode="json") for item in await control.approvals()]

    @app.post("/api/pause/global")
    async def pause_global(
        payload: PauseRequest,
        lemonbot_session: Annotated[str | None, Cookie()] = None,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        session = require_session(lemonbot_session)
        require_csrf(session, x_csrf_token)
        return (await control.set_pause(None, payload.paused)).model_dump(mode="json")

    @app.post("/api/pause/{channel}")
    async def pause_channel(
        channel: Literal["wecom", "wechat_uia"],
        payload: PauseRequest,
        lemonbot_session: Annotated[str | None, Cookie()] = None,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        session = require_session(lemonbot_session)
        require_csrf(session, x_csrf_token)
        return (await control.set_pause(channel, payload.paused)).model_dump(mode="json")

    @app.post("/api/emergency-stop")
    async def emergency_stop(
        lemonbot_session: Annotated[str | None, Cookie()] = None,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, object]:
        session = require_session(lemonbot_session)
        require_csrf(session, x_csrf_token)
        return (await control.emergency_stop()).model_dump(mode="json")

    @app.post("/api/approvals/{approval_id}")
    async def decide_approval(
        approval_id: str,
        payload: ApprovalRequest,
        lemonbot_session: Annotated[str | None, Cookie()] = None,
        x_csrf_token: Annotated[str | None, Header()] = None,
    ) -> dict[str, bool]:
        session = require_session(lemonbot_session)
        require_csrf(session, x_csrf_token)
        return {"ok": await control.decide_approval(approval_id, payload.decision)}

    return app


_LOGIN_HTML = """<!doctype html>
<html lang="zh-CN"><head><meta charset="utf-8"><title>Lemonbot 本地管理</title>
<meta name="referrer" content="no-referrer"><style>
body{font:16px system-ui;max-width:820px;margin:5vh auto;padding:2rem;background:#111;color:#eee}
button{padding:.6rem .9rem;margin:.2rem}button.danger{background:#8b1e1e;color:#fff}
pre,.card{white-space:pre-wrap;background:#222;padding:1rem;border-radius:.4rem}.card{margin:.7rem 0}
</style></head><body><h1>Lemonbot</h1><p id="state">正在验证本机一次性令牌……</p>
<pre id="status"></pre><section id="controls" hidden><h2>出站控制</h2>
<button data-pause="global:1">暂停全部</button><button data-pause="global:0">恢复全部</button>
<button data-pause="wecom:1">暂停企业微信</button><button data-pause="wecom:0">恢复企业微信</button>
<button data-pause="wechat_uia:1">暂停个人微信</button><button data-pause="wechat_uia:0">恢复个人微信</button>
<button id="stop" class="danger">紧急停止</button></section>
<section id="approval-section" hidden><h2>一次性审批</h2><div id="approvals"></div></section><script>
const state=document.getElementById('state'), status=document.getElementById('status');
let csrf=sessionStorage.getItem('csrf');
async function mutate(url, body){return fetch(url,{method:'POST',headers:{'content-type':'application/json','x-csrf-token':csrf},body:body?JSON.stringify(body):undefined})}
async function refresh(){
 const r=await fetch('/api/status');if(!r.ok){state.textContent='请从托盘重新打开管理台';return false}
 state.textContent='仅限本机访问';status.textContent=JSON.stringify(await r.json(),null,2);
 document.getElementById('controls').hidden=false;const ar=await fetch('/api/approvals');
 if(ar.ok){const root=document.getElementById('approvals');root.replaceChildren();const items=await ar.json();
  document.getElementById('approval-section').hidden=false;
  if(!items.length){root.textContent='没有待审批动作。'}
  for(const item of items){const card=document.createElement('div');card.className='card';
   const text=document.createElement('div');text.textContent=`${item.action_type} · ${item.channel}/${item.chat_id}\n${item.summary}\n过期：${item.expires_at}`;card.append(text);
   for(const decision of ['approve_once','deny']){const b=document.createElement('button');b.textContent=decision==='approve_once'?'批准一次':'拒绝';
    if(decision==='approve_once')b.className='danger';b.onclick=async()=>{if(decision==='approve_once'&&!confirm('只执行这里显示的精确动作一次？'))return;
     await mutate(`/api/approvals/${encodeURIComponent(item.approval_id)}`,{decision});await refresh()};card.append(b)}root.append(card)}}return true}
async function boot(){
 const token=location.hash.slice(1); history.replaceState(null,'',location.pathname);
 if(token){const r=await fetch('/auth/exchange',{method:'POST',headers:{'content-type':'application/json'},body:JSON.stringify({token})});
   if(!r.ok){state.textContent='令牌无效或已使用';return} csrf=(await r.json()).csrf;sessionStorage.setItem('csrf',csrf)}
 if(!csrf){state.textContent='请从托盘重新打开管理台';return}if(!await refresh())return;
 for(const b of document.querySelectorAll('[data-pause]'))b.onclick=async()=>{const [channel,value]=b.dataset.pause.split(':');
  await mutate(`/api/pause/${channel==='global'?'global':encodeURIComponent(channel)}`,{paused:value==='1'});await refresh()};
 document.getElementById('stop').onclick=async()=>{if(!confirm('立即停止全部出站操作，并要求重启后才能恢复？'))return;
  await mutate('/api/emergency-stop');await refresh()};}
boot();
</script></body></html>"""

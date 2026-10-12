#!/usr/bin/env python3
"""Authenticated localhost status/pause/resume without printing credentials."""

from __future__ import annotations

import argparse
import asyncio
import hashlib
import json
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from lemonbot.config import RuntimePaths
from lemonbot.config.paths import default_wechat_runtime_config
from lemonbot.config.settings import load_settings
from lemonbot.connectors.wechat_agent import CHANNEL
from lemonbot.connectors.wechat_agent_state import private_read, private_write


async def control(action: str, config: Path) -> None:
    settings = load_settings(config)
    paths = RuntimePaths.from_settings(settings)
    parts = urlsplit(
        private_read(paths.lock_file.parent / f"{settings.profile}.admin-url").decode()
    )
    if parts.scheme != "http" or parts.hostname != "127.0.0.1" or not parts.fragment:
        raise ValueError("InvalidAdminURL")
    if parts.port != settings.admin.port or parts.path != "/login":
        raise ValueError("InvalidAdminURL")
    origin = f"http://127.0.0.1:{settings.admin.port}"
    cache = config.parent / "conversation-admin-session.json"
    saved = json.loads(private_read(cache)) if cache.exists() else {}
    fingerprint = hashlib.sha256(parts.fragment.encode()).hexdigest()
    async with httpx.AsyncClient(base_url=origin, trust_env=False, timeout=90) as client:
        if saved.get("bootstrap_ref") == fingerprint:
            client.cookies.set("lemonbot_session", saved["session"])
            csrf = saved["csrf"]
        else:
            response = await client.post(
                "/auth/exchange", json={"token": parts.fragment}, headers={"Origin": origin}
            )
            response.raise_for_status()
            csrf = response.json()["csrf"]
            private_write(
                cache,
                json.dumps(
                    {
                        "bootstrap_ref": fingerprint,
                        "session": client.cookies.get("lemonbot_session"),
                        "csrf": csrf,
                    }
                ).encode(),
            )
        if action == "status":
            response = await client.get("/api/status")
        else:
            response = await client.post(
                "/api/pause/" + CHANNEL,
                json={"paused": action == "pause"},
                headers={"Origin": origin, "X-CSRF-Token": csrf},
            )
        if response.status_code != 200:
            print(json.dumps({"action": action, "status_code": response.status_code, "ok": False}))
            raise SystemExit(1)
        state = response.json()
        print(
            json.dumps(
                {
                    "action": action,
                    "ok": True,
                    "paused": state["global_paused"] or state["channel_pauses"].get(CHANNEL, True),
                    "reason": state.get("channel_pause_reasons", {}).get(CHANNEL),
                    "target_pause_reasons": list(state.get("conversation_pauses", {}).values()),
                    "queue_depth": state["queue_depth"],
                    "unknown_outbox": state["unknown_outbox"],
                    "model_account": state.get("model_account"),
                }
            )
        )


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("action", choices=("status", "pause", "resume"))
    parser.add_argument(
        "--config",
        type=Path,
        default=default_wechat_runtime_config(),
    )
    args = parser.parse_args()
    try:
        asyncio.run(control(args.action, args.config))
    except Exception as error:
        print(json.dumps({"ok": False, "error": type(error).__name__}))
        raise SystemExit(1) from None


if __name__ == "__main__":
    main()

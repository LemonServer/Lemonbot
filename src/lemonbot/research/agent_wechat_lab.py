"""Run the isolated canary-only lab: python -m lemonbot.research.agent_wechat_lab."""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import secrets
import stat
import tempfile
from datetime import timedelta
from pathlib import Path
from typing import Any

import httpx

from lemonbot.connectors.wechat_agent_state import (
    DEFAULT_STATE as DEFAULT_STATE,
)
from lemonbot.connectors.wechat_agent_state import (
    load_binding as load_binding,
)
from lemonbot.connectors.wechat_agent_state import (
    prepare_state as prepare_state,
)
from lemonbot.connectors.wechat_agent_state import (
    private_read as private_read,
)
from lemonbot.connectors.wechat_agent_state import (
    private_write as private_write,
)
from lemonbot.connectors.wechat_agent_state import (
    save_binding as save_binding,
)
from lemonbot.domain.models import DeliveryStatus
from lemonbot.research.agent_wechat import (
    CHANNEL,
    AgentWechatConnector,
    AgentWechatHTTP,
    LabPaused,
    enroll,
    reply_for,
)
from lemonbot.runtime_lock import AlreadyRunningError, RuntimeLock
from lemonbot.storage import CoreRepository, Database


class LabRunner:
    """Uses the existing durable broker states, without a model or product runtime."""

    def __init__(self, connector: AgentWechatConnector, repository: CoreRepository) -> None:
        self.connector = connector
        self.repository = repository

    async def initialize(self) -> dict[str, int]:
        recovered = await self.repository.recover_interrupted(stale_after=timedelta(0))
        await self.connector.baseline()
        return recovered

    async def process_pending(self) -> int:
        processed = 0
        while item := await self.repository.claim_next_inbox(CHANNEL):
            try:
                if item.event.chat_id != self.connector.binding.chat_ref:
                    raise LabPaused("PersistedChatChanged")
                message = reply_for(item.event)
                await self.repository.create_outbox(message)
                await self.repository.complete_inbox(item.id)
                processed += 1
            except LabPaused as error:
                await self.repository.fail_inbox(item.id, str(error), retryable=False)
                raise
        return processed

    async def dispatch_pending(self) -> list[str]:
        outcomes = []
        while item := await self.repository.reserve_next_outbox(CHANNEL):
            # Reservation is safe to recover; dispatching is never automatically retried.
            await self.connector.guard()
            if not await self.repository.mark_dispatching(item.id):
                raise LabPaused("DispatchCommitFailed")
            try:
                receipt = await self.connector.deliver(item.message)
                await self.repository.apply_receipt(item.id, receipt)
            except BaseException:
                await self.repository.mark_outbox_unknown(item.id, "InterruptedNoRetry")
                raise
            outcomes.append(receipt.status.value)
            if receipt.status is DeliveryStatus.UNKNOWN:
                raise LabPaused(self.connector.paused or "UnknownDeliveryNoRetry")
        return outcomes

    async def tick(self) -> dict[str, Any]:
        events = await self.connector.poll()
        inserted = 0
        for event in events:
            inserted += int(await self.repository.record_inbound(event))
        processed = await self.process_pending()
        outcomes = await self.dispatch_pending()
        return {"new_inbound": inserted, "processed": processed, "deliveries": outcomes}


class SimulatedWechat:
    """Synthetic HTTP fixture, never evidence about a real WeChat session."""

    def __init__(self) -> None:
        self.rows: list[dict[str, Any]] = []
        self.posts = 0
        self.title = "testing"
        self.account = "synthetic-account"
        self.chat = "synthetic@chatroom"
        self.next_id = 1
        self.send_timeout = False

    def append(self, text: str, *, is_self: bool = False) -> dict[str, Any]:
        row = {
            "localId": self.next_id,
            "chatId": self.chat,
            "sender": self.account if is_self else "synthetic-peer",
            "isSelf": is_self,
            "type": 1,
            "content": text,
            "timestamp": "2026-10-01T00:00:00+00:00",
        }
        self.next_id += 1
        self.rows.insert(0, row)
        return row

    def handle(self, request: httpx.Request) -> httpx.Response:
        path = request.url.path
        if path == "/api/status/auth":
            data: Any = {"status": "logged_in", "loggedInUser": self.account}
        elif path == "/api/chats":
            data = [{"name": "testing", "id": self.chat}]
        elif path == "/api/debug/a11y":
            data = {
                "tree": {
                    "role": "frame",
                    "name": "synthetic-wechat",
                    "bounds": {"x": 0, "y": 0, "width": 1024, "height": 800},
                    "children": [
                        {
                            "role": "list",
                            "name": "Chats",
                            "bounds": {"x": 0, "y": 0, "width": 272, "height": 800},
                        },
                        {
                            "role": "label",
                            "name": self.title,
                            "bounds": {"x": 300, "y": 20, "width": 100, "height": 30},
                        },
                    ],
                },
            }
        elif path == "/api/messages/send" and request.method == "POST":
            self.posts += 1
            body = json.loads(request.content)
            if body["chatId"] != self.chat:
                return httpx.Response(400)
            self.append(body["text"], is_self=True)
            if self.send_timeout:
                raise httpx.ReadTimeout("synthetic ambiguous timeout", request=request)
            data = {"success": True}
        elif path.startswith("/api/messages/"):
            offset = int(request.url.params["offset"])
            limit = int(request.url.params["limit"])
            data = self.rows[offset : offset + limit]
        else:
            return httpx.Response(404)
        return httpx.Response(200, json=data)

    def http(self) -> AgentWechatHTTP:
        return AgentWechatHTTP("synthetic-token", transport=httpx.MockTransport(self.handle))


async def demo(state: Path) -> dict[str, Any]:
    key = prepare_state(state)
    mock = SimulatedWechat()
    http = mock.http()
    binding = await enroll(http, key)
    save_binding(state / "binding.json", binding)
    database = Database.from_path(state / "lab.db")
    await database.initialise()
    connector = AgentWechatConnector(http, binding, key, readback_seconds=0)
    repository = CoreRepository(database)
    runner = LabRunner(connector, repository)
    try:
        await runner.initialize()
        command = "LB26_E2E_" + "A" * 24
        row = mock.append(command)
        first = await runner.tick()
        await runner.tick()  # self echo must not cause another reply
        await connector.close()
        await database.close()
        database = Database.from_path(state / "lab.db")
        repository = CoreRepository(database)
        connector = AgentWechatConnector(mock.http(), binding, key, readback_seconds=0)
        runner = LabRunner(connector, repository)
        await runner.initialize()
        restarted = await runner.tick()
        # A genuine redelivery after restart has the same stable event identity.
        event = connector.event(row)
        assert event is not None
        duplicate_inserted = await repository.record_inbound(event)
        await runner.process_pending()
        await runner.dispatch_pending()
        mock.send_timeout = True
        ambiguous = mock.append("LB26_E2E_" + "B" * 24)
        try:
            await runner.tick()
        except LabPaused:
            pass
        ambiguous_event = connector.event(ambiguous)
        assert ambiguous_event is not None
        unknown_id = reply_for(ambiguous_event).message_id
        await connector.close()
        await database.close()
        database = Database.from_path(state / "lab.db")
        repository = CoreRepository(database)
        connector = AgentWechatConnector(mock.http(), binding, key, readback_seconds=0)
        runner = LabRunner(connector, repository)
        await runner.initialize()
        await runner.tick()
        unknown = await repository.outbox_state(unknown_id)
        ok = (
            first["deliveries"] == ["acknowledged"]
            and restarted["new_inbound"] == 0
            and not duplicate_inserted
            and mock.posts == 2
            and unknown is not None
            and unknown.value == "unknown"
        )
        return {
            "mode": "simulated_http_not_live",
            "ok": ok,
            "first": first,
            "restart": restarted,
            "duplicate_inserted": duplicate_inserted,
            "post_count": mock.posts,
            "unknown_not_retried": unknown is not None and unknown.value == "unknown",
            "model_calls": 0,
        }
    finally:
        await connector.close()
        await database.close()


async def run_live(args: argparse.Namespace) -> None:
    state = args.state_dir
    key = prepare_state(state)
    token = private_read(args.token_file).decode().strip()
    http = AgentWechatHTTP(token)
    connector: AgentWechatConnector | None = None
    database: Database | None = None
    try:
        if args.command == "enroll":
            binding = await enroll(http, key)
            save_binding(state / "binding.json", binding)
            print(
                json.dumps(
                    {
                        "ok": True,
                        "enrolled": "testing",
                        "header_path": binding.header_path,
                    }
                )
            )
            return
        if not args.confirm_testing:
            raise LabPaused("ExplicitTestingConfirmationRequired")
        if (state / "paused.json").exists() and not args.resume:
            raise LabPaused("ExplicitResumeRequired")
        binding = load_binding(state / "binding.json")
        connector = AgentWechatConnector(
            http,
            binding,
            key,
            run_id="AGW_" + secrets.token_hex(8).upper(),
            diagnostic=lambda value: print(json.dumps({"send_diagnostic": value}), flush=True),
        )
        database_path = state / "lab.db"
        fd = os.open(database_path, os.O_RDWR | os.O_CREAT | os.O_NOFOLLOW, 0o600)
        try:
            info = os.fstat(fd)
            if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid():
                raise LabPaused("UnsafeDatabase")
            os.fchmod(fd, 0o600)
        finally:
            os.close(fd)
        database = Database.from_path(database_path)
        await database.initialise()
        runner = LabRunner(connector, CoreRepository(database))
        recovered = await runner.initialize()
        (state / "paused.json").unlink(missing_ok=True)
        print(json.dumps({"mode": "live_testing_lab", "ready": True, "recovered": recovered}))
        deadline = asyncio.get_running_loop().time() + args.seconds
        while asyncio.get_running_loop().time() < deadline:
            result = await runner.tick()
            if result["new_inbound"] or result["deliveries"]:
                print(json.dumps(result), flush=True)
            await asyncio.sleep(2)
    except LabPaused as error:
        private_write(state / "paused.json", json.dumps({"code": str(error)}).encode())
        raise
    finally:
        if connector is not None:
            await connector.close()
        else:
            await http.close()
        if database is not None:
            await database.close()


def main() -> None:
    os.umask(0o077)
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("command", choices=("demo", "enroll", "run", "marker"))
    parser.add_argument("--state-dir", type=Path)
    parser.add_argument("--token-file", type=Path)
    parser.add_argument("--confirm-testing", action="store_true")
    parser.add_argument("--resume", action="store_true")
    parser.add_argument("--seconds", type=int, default=1800)
    args = parser.parse_args()
    if args.command == "marker":
        print("LB26_E2E_" + secrets.token_hex(12).upper())
        return
    if not 1 <= args.seconds <= 5400:
        parser.error("seconds must be between 1 and 5400")
    try:
        if args.command == "demo":
            if args.state_dir is None:
                with tempfile.TemporaryDirectory(prefix="lemonbot-agent-demo-") as directory:
                    state = Path(directory)
                    with RuntimeLock(state / "run.lock", persistent=True):
                        result = asyncio.run(demo(state))
            else:
                args.state_dir.mkdir(parents=True, exist_ok=True, mode=0o700)
                if any(args.state_dir.iterdir()):
                    raise LabPaused("DemoRequiresEmptyDirectory")
                with RuntimeLock(args.state_dir / "run.lock", persistent=True):
                    result = asyncio.run(demo(args.state_dir))
            print(json.dumps(result))
            if not result["ok"]:
                raise SystemExit(1)
        else:
            if args.token_file is None:
                parser.error("token-file is required for live commands")
            args.state_dir = args.state_dir or DEFAULT_STATE
            prepare_state(args.state_dir)
            with RuntimeLock(args.state_dir / "run.lock", persistent=True):
                asyncio.run(run_live(args))
    except (LabPaused, AlreadyRunningError) as error:
        print(json.dumps({"ok": False, "paused": str(error)}))
        raise SystemExit(2) from None
    except (OSError, ValueError, httpx.HTTPError):
        print(json.dumps({"ok": False, "paused": "LocalStateOrTransportError"}))
        raise SystemExit(2) from None
    except KeyboardInterrupt:
        print(json.dumps({"stopped": True, "interrupted_dispatch_not_retried": True}))


if __name__ == "__main__":
    main()

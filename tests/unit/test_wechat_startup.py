from __future__ import annotations

import json
from types import SimpleNamespace
from urllib.parse import unquote

import httpx
import pytest

from lemonbot.connectors.wechat_agent import reference
from lemonbot.connectors.wechat_agent_startup import report_conversations, report_sync_failure


@pytest.mark.parametrize("fault", [None, "missing", "paused", "http", "binding"])
async def test_startup_summary_distinguishes_readable_and_enabled_without_leaking_data(
    capsys,
    fault: str | None,
) -> None:
    key = b"x" * 32
    account = "private-account"
    children = {
        "ref-a": SimpleNamespace(
            binding=SimpleNamespace(chat_id="group@chatroom"),
            target=SimpleNamespace(name="testing", is_group=True),
        ),
        "ref-b": SimpleNamespace(
            binding=SimpleNamespace(chat_id="private-id"),
            target=SimpleNamespace(name="name\nwith newline", is_group=False),
        ),
    }

    class HTTP:
        async def request(self, method, path):
            if path == "/api/status/auth":
                return {"status": "logged_in", "loggedInUser": account}
            child = next(c for c in children.values() if c.binding.chat_id in unquote(path))
            return {
                "id": child.binding.chat_id,
                "name": "changed" if fault == "binding" else child.target.name,
                "isGroup": child.target.is_group,
            }

    def metadata(request):
        assert json.loads(request.content)["anchors"] == {
            "group@chatroom": None,
            "private-id": None,
        }
        return httpx.Response(
            503 if fault == "http" else 200,
            json={
                "accountVerified": True,
                "targets": [
                    {"chatId": "group@chatroom", "error": None},
                    {
                        "chatId": "private-id",
                        "error": "MessageTableMissing" if fault == "missing" else None,
                    },
                ],
            },
        )

    async with httpx.AsyncClient(
        base_url="http://localhost", transport=httpx.MockTransport(metadata)
    ) as client:
        transport = SimpleNamespace(
            key=key,
            binding=SimpleNamespace(account_ref=reference(key, "account", account)),
            http=HTTP(),
            metadata=client,
            children=children,
            target_pauses={},
            paused=None,
        )
        await report_conversations(
            transport,
            ready=fault != "paused",
            pause_reason="ManualPause" if fault == "paused" else None,
        )
    output = capsys.readouterr().out
    rows = [json.loads(line.removeprefix("wechat_startup ")) for line in output.splitlines()]
    assert len(rows) == 3
    assert rows[1]["name"] == "name\nwith newline"
    assert rows[-1]["all_available"] is (fault is None)
    assert rows[-1]["readable"] == (
        2 if fault in {None, "paused"} else 1 if fault == "missing" else 0
    )
    if fault == "paused":
        assert rows[-1]["enabled"] == 0
        assert rows[0]["reason"] == "ManualPause"
    assert "private-account" not in output and "private-id" not in output


async def test_sync_failure_identifies_missing_name_and_does_not_claim_others_unreadable(
    capsys,
) -> None:
    class HTTP:
        async def request(self, *_args):
            return [{"name": "present", "isGroup": False}]

    await report_sync_failure(
        HTTP(), [("present", False), ("missing", True)], "WhitelistTargetNotFound"
    )
    rows = [
        json.loads(x.removeprefix("wechat_startup ")) for x in capsys.readouterr().out.splitlines()
    ]
    assert rows[0]["resolved"] is True
    assert rows[0]["readable"] is None
    assert rows[0]["reason"] == "StartupBlockedByWhitelist"
    assert rows[1]["resolved"] is False
    assert rows[1]["reason"] == "WhitelistTargetNotFound"
    assert rows[2]["startup_failed"] is True
    assert rows[2]["all_available"] is False

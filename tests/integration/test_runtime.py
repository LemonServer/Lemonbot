from __future__ import annotations

import hashlib
from pathlib import Path

import pytest

from lemonbot.config.settings import AppSettings
from lemonbot.connectors import FakeConnector
from lemonbot.domain import InboundEvent, OutboundMessage, OutboxState
from lemonbot.orchestration import PipelineStatus
from lemonbot.runtime import LemonbotRuntime
from lemonbot.tools.mcp import MCPError, MCPStdioClient


async def test_fake_runtime_vertical_slice(tmp_path: Path) -> None:
    raw = AppSettings().model_dump(mode="python")
    raw["runtime"]["data_root"] = str(tmp_path)
    raw["models"]["provider"] = "fake"
    settings = AppSettings.model_validate(raw)
    runtime = LemonbotRuntime(settings)
    await runtime.initialize()
    try:
        assert isinstance(runtime.connector, FakeConnector)
        assert runtime.pipeline is not None
        assert runtime.pipeline.memory_context is not None
        assert runtime.pipeline.memory_derivation is not None
        await runtime.repository.set_allowlisted("fake", "chat-1")
        event = InboundEvent(
            channel="fake",
            event_id="runtime-event-1",
            chat_id="chat-1",
            sender_id="owner",
            text="hello",
        )
        assert (await runtime.pipeline.ingest(event)).status is PipelineStatus.QUEUED
        assert (await runtime.pipeline.process_once("fake")).status is PipelineStatus.COMPLETED
        assert (
            await runtime.pipeline.dispatch_once(runtime.connector, channel="fake")
        ).status is PipelineStatus.ACKNOWLEDGED
        assert runtime.connector.delivered_messages[0].text == "AI: hello"
    finally:
        await runtime.close()


def _mcp_server_config(executable: Path, digest: str) -> dict[str, object]:
    schema = {
        "type": "object",
        "additionalProperties": False,
        "properties": {"query": {"type": "string"}},
        "required": ["query"],
    }
    return {
        "name": "catalog",
        "enabled": True,
        "executable": str(executable),
        "arguments": ["--stdio"],
        "executable_sha256": digest,
        "working_directory": str(executable.parent),
        "expected_server_name": "catalog-server",
        "expected_server_version": "1.0.0",
        "tools": {
            "read": {
                "remote_name": "catalog.read",
                "description": "Read the catalog.",
                "input_schema": schema,
                "read_only": True,
                "enabled": True,
            },
            "write": {
                "remote_name": "catalog.write",
                "description": "Write one catalog entry.",
                "input_schema": schema,
                "read_only": False,
                "enabled": True,
            },
            "disabled": {
                "remote_name": "catalog.disabled",
                "description": "Disabled capability.",
                "input_schema": schema,
                "read_only": True,
                "enabled": False,
            },
        },
    }


async def test_runtime_registers_only_enabled_hash_pinned_mcp_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    executable = tmp_path / "catalog.exe"
    executable.write_bytes(b"fixed catalog server")
    digest = hashlib.sha256(executable.read_bytes()).hexdigest()
    raw = AppSettings().model_dump(mode="python")
    raw["runtime"]["data_root"] = str(tmp_path / "data")
    raw["models"]["provider"] = "fake"
    raw["mcp"] = {
        "enabled": True,
        "servers": [_mcp_server_config(executable, digest)],
    }
    settings = AppSettings.model_validate(raw)

    async def verify_only(self: MCPStdioClient) -> None:
        self.verify_pin()

    monkeypatch.setattr(MCPStdioClient, "start", verify_only)
    runtime = LemonbotRuntime(settings)
    await runtime.initialize()
    try:
        assert set(runtime._tools) == {"mcp.catalog.read", "mcp.catalog.write"}
        read_manifest = runtime._tools["mcp.catalog.read"].manifest()
        write_manifest = runtime._tools["mcp.catalog.write"].manifest()
        assert read_manifest.action_kind == "mcp_read"
        assert read_manifest.required_scopes.issubset(runtime._tool_scopes)
        assert write_manifest.action_kind == "mcp_write"
        assert not write_manifest.required_scopes.issubset(runtime._tool_scopes)
    finally:
        await runtime.close()


async def test_runtime_fails_closed_when_mcp_executable_pin_is_wrong(tmp_path: Path) -> None:
    executable = tmp_path / "catalog.exe"
    executable.write_bytes(b"actual server")
    raw = AppSettings().model_dump(mode="python")
    raw["runtime"]["data_root"] = str(tmp_path / "data")
    raw["models"]["provider"] = "fake"
    raw["mcp"] = {
        "enabled": True,
        "servers": [_mcp_server_config(executable, "0" * 64)],
    }
    runtime = LemonbotRuntime(AppSettings.model_validate(raw))

    with pytest.raises(MCPError, match="hash does not match"):
        await runtime.initialize()

    await runtime.close()


async def test_runtime_immediately_recovers_fresh_precommit_states(tmp_path: Path) -> None:
    raw = AppSettings().model_dump(mode="python")
    raw["runtime"]["data_root"] = str(tmp_path)
    raw["models"]["provider"] = "fake"
    settings = AppSettings.model_validate(raw)

    first = LemonbotRuntime(settings)
    await first.initialize()
    try:
        await first.repository.record_inbound(
            InboundEvent(
                channel="fake",
                event_id="fresh-processing",
                chat_id="chat-1",
                sender_id="user-1",
                text="recover me",
            )
        )
        assert await first.repository.claim_next_inbox("fake") is not None
        outbound = await first.repository.create_outbox(
            OutboundMessage(channel="fake", chat_id="chat-1", text="safe to retry")
        )
        assert await first.repository.reserve_next_outbox("fake") is not None
    finally:
        await first.close()

    restarted = LemonbotRuntime(settings)
    await restarted.initialize()
    try:
        assert await restarted.repository.claim_next_inbox("fake") is not None
        recovered_outbox = await restarted.repository.reserve_next_outbox("fake")
        assert recovered_outbox is not None
        assert recovered_outbox.message.message_id == outbound.message.message_id
        assert (
            await restarted.repository.outbox_state(outbound.message.message_id)
            is OutboxState.RESERVED
        )
    finally:
        await restarted.close()

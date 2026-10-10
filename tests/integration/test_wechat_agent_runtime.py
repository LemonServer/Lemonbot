from __future__ import annotations

import asyncio
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from lemonbot.config.settings import AppSettings
from lemonbot.connectors.wechat_agent import CHANNEL, enroll
from lemonbot.connectors.wechat_agent_runtime import RuntimeAgentWechatConnector
from lemonbot.connectors.wechat_agent_state import prepare_state, private_write, save_binding
from lemonbot.domain.models import OutboxState
from lemonbot.orchestration import FakeModelBackend, PipelineStatus
from lemonbot.policy import DeterministicPolicy
from lemonbot.research.agent_wechat_lab import SimulatedWechat
from lemonbot.runtime import LemonbotRuntime
from lemonbot.runtime_lock import AlreadyRunningError, RuntimeLock

COMMAND = "LB26_E2E_" + "C" * 24


async def setup_runtime(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    mock: SimulatedWechat,
) -> LemonbotRuntime:
    state = tmp_path / "binding"
    key = prepare_state(state)
    http = mock.http()
    save_binding(state / "binding.json", await enroll(http, key))
    await http.close()
    private_write(state / "token", b"synthetic-token")
    raw = AppSettings().model_dump(mode="python")
    raw["profile"] = "lab"
    raw["runtime"] = {"connector": "wechat_agent", "data_root": str(tmp_path / "runtime")}
    raw["wechat_agent"] = {
        "enabled": True,
        "state_dir": str(state),
        "token_file": str(state / "token"),
    }
    monkeypatch.setattr("lemonbot.runtime.AgentWechatHTTP", lambda token: mock.http())
    runtime = LemonbotRuntime(AppSettings.model_validate(raw))
    await runtime.initialize()
    assert isinstance(runtime.connector, RuntimeAgentWechatConnector)
    runtime.connector.transport.readback_seconds = 0
    assert runtime.pipeline is not None
    assert isinstance(runtime.pipeline.policy, DeterministicPolicy)
    runtime.pipeline.policy.clock = lambda: datetime.now(UTC).replace(hour=4)
    return runtime


async def ingest_new(runtime: LemonbotRuntime) -> None:
    assert isinstance(runtime.connector, RuntimeAgentWechatConnector)
    assert runtime.pipeline is not None
    for event in await runtime.connector.transport.poll():
        await runtime.pipeline.ingest(event)


async def test_runtime_canary_uses_existing_outbox_without_model_or_tools(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = SimulatedWechat()
    runtime = await setup_runtime(tmp_path, monkeypatch, mock)
    try:
        assert runtime.pipeline is not None
        assert runtime.pipeline.memory_context is None
        assert runtime.pipeline.memory_derivation is None
        assert runtime.proactive_runner is None
        assert runtime.pipeline.tools == {}
        mock.append(COMMAND)
        mock.append("LB26_E2E_" + "D" * 24, is_self=True)
        await ingest_new(runtime)
        result = await runtime.pipeline.process_once(CHANNEL)
        assert result.status is PipelineStatus.COMPLETED  # disabled model would fail if called
        assert (await runtime.pipeline.process_once(CHANNEL)).status is PipelineStatus.IDLE
        assert (
            await runtime.pipeline.dispatch_once(runtime.connector, channel=CHANNEL)
        ).status is PipelineStatus.ACKNOWLEDGED
        assert mock.posts == 1
        assert mock.rows[0]["content"] == COMMAND.replace("E2E", "ACK")
        await ingest_new(runtime)
        assert (await runtime.pipeline.process_once(CHANNEL)).status is PipelineStatus.IDLE
        # A normal program restart establishes a new baseline and never replays the old command.
        await runtime.close()
        runtime = await setup_runtime(tmp_path, monkeypatch, mock)
        await ingest_new(runtime)
        assert runtime.pipeline is not None
        assert (await runtime.pipeline.process_once(CHANNEL)).status is PipelineStatus.IDLE
        assert mock.posts == 1
    finally:
        await runtime.close()


async def test_unknown_is_sticky_across_restart_and_explicit_resume_does_not_retry(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = SimulatedWechat()
    runtime = await setup_runtime(tmp_path, monkeypatch, mock)
    try:
        assert runtime.pipeline is not None
        assert isinstance(runtime.connector, RuntimeAgentWechatConnector)
        mock.send_timeout = True
        mock.append(COMMAND)
        await ingest_new(runtime)
        await runtime.pipeline.process_once(CHANNEL)
        assert (
            await runtime.pipeline.dispatch_once(runtime.connector, channel=CHANNEL)
        ).status is PipelineStatus.UNKNOWN
        assert runtime.connector.pause_file.exists()
        assert await runtime.repository.is_paused(CHANNEL)
        reason = runtime.connector.pause_reason
        await runtime.close()
        runtime = await setup_runtime(tmp_path, monkeypatch, mock)
        assert isinstance(runtime.connector, RuntimeAgentWechatConnector)
        assert runtime.connector.pause_reason == reason
        assert not runtime.connector.ready
        await runtime.connector.resume()
        assert runtime.pipeline is not None
        assert (
            await runtime.pipeline.dispatch_once(runtime.connector, channel=CHANNEL)
        ).status is PipelineStatus.IDLE
        assert mock.posts == 1
    finally:
        await runtime.close()


async def test_foreground_failure_keeps_event_consumer_alive_and_requires_valid_resume(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = SimulatedWechat()
    runtime = await setup_runtime(tmp_path, monkeypatch, mock)
    try:
        assert isinstance(runtime.connector, RuntimeAgentWechatConnector)
        runtime.connector.transport.poll_seconds = 0.01
        mock.title = "another chat"
        consumer = asyncio.create_task(runtime._consume_loop())
        for _ in range(100):
            if runtime.connector.pause_reason:
                break
            await asyncio.sleep(0.01)
        assert runtime.connector.pause_reason == "TestingNotForeground"
        assert not consumer.done()
        with pytest.raises(RuntimeError, match="verification failed"):
            await runtime.connector.resume()
        assert await runtime.repository.is_paused(CHANNEL)
        mock.title = "testing"
        mock.append(COMMAND)  # baseline excludes messages accumulated while paused
        await runtime.connector.resume()
        assert runtime.connector.ready
        assert await runtime.connector.transport.poll() == []
        consumer.cancel()
        await asyncio.gather(consumer, return_exceptions=True)
    finally:
        await runtime.close()


async def test_crash_during_dispatch_recovers_unknown_and_pauses(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = SimulatedWechat()
    runtime = await setup_runtime(tmp_path, monkeypatch, mock)
    try:
        assert runtime.pipeline is not None
        mock.append(COMMAND)
        await ingest_new(runtime)
        await runtime.pipeline.process_once(CHANNEL)
        reserved = await runtime.repository.reserve_next_outbox(CHANNEL)
        assert reserved is not None
        await runtime.repository.mark_dispatching(reserved.id)
        identifier = reserved.message.message_id
        await runtime.close()
        runtime = await setup_runtime(tmp_path, monkeypatch, mock)
        assert await runtime.repository.outbox_state(identifier) is OutboxState.UNKNOWN
        assert isinstance(runtime.connector, RuntimeAgentWechatConnector)
        assert runtime.connector.pause_reason == "InterruptedSend"
        assert mock.posts == 0
    finally:
        await runtime.close()


def test_shared_transport_lock_cannot_be_bypassed_after_release(tmp_path: Path) -> None:
    path = tmp_path / "run.lock"
    with RuntimeLock(path, persistent=True):
        with pytest.raises(AlreadyRunningError):
            with RuntimeLock(path, persistent=True):
                pass
    inode = path.stat().st_ino
    with RuntimeLock(path, persistent=True):
        assert path.stat().st_ino == inode
        with pytest.raises(AlreadyRunningError):
            with RuntimeLock(path, persistent=True):
                pass


@pytest.mark.parametrize("change", ["prod", "model", "tool", "not_enabled", "no_data"])
def test_agent_runtime_configuration_is_restricted(change: str, tmp_path: Path) -> None:
    raw = AppSettings().model_dump(mode="python")
    raw["profile"] = "lab"
    raw["runtime"] = {"connector": "wechat_agent", "data_root": str(tmp_path / "independent")}
    raw["wechat_agent"] = {
        "enabled": True,
        "token_file": str(tmp_path / "token"),
        "state_dir": str(tmp_path / "state"),
    }
    if change == "prod":
        raw["profile"] = "prod"
    elif change == "model":
        raw["models"]["provider"] = "fake"
    elif change == "tool":
        raw["browser"]["enabled"] = True
    elif change == "not_enabled":
        raw["wechat_agent"]["enabled"] = False
    else:
        raw["runtime"]["data_root"] = ""
    with pytest.raises(ValidationError):
        AppSettings.model_validate(raw)


@pytest.mark.parametrize("restriction", ["allowlist", "pause", "quiet", "rate"])
async def test_canary_remains_subject_to_policy_without_model_calls(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    restriction: str,
) -> None:
    mock = SimulatedWechat()
    runtime = await setup_runtime(tmp_path, monkeypatch, mock)
    try:
        assert runtime.pipeline is not None
        assert isinstance(runtime.connector, RuntimeAgentWechatConnector)
        probe = FakeModelBackend(["must not be generated"])
        runtime.pipeline.model = probe
        if restriction == "allowlist":
            await runtime.repository.set_allowlisted(CHANNEL, runtime.connector.chat_ref, False)
        elif restriction == "pause":
            await runtime.repository.set_paused(paused=True)
        elif restriction == "quiet":
            assert isinstance(runtime.pipeline.policy, DeterministicPolicy)
            runtime.pipeline.policy.clock = lambda: datetime.now(UTC).replace(hour=16)
        else:
            # Rate accounting must use the actual creation clock; the fixed
            # daytime clock used by quiet-hour tests can be hours ahead.
            runtime.pipeline.policy.clock = lambda: datetime.now(UTC)
            runtime.pipeline.policy.config = runtime.pipeline.policy.config.model_copy(
                update={"quiet_enabled": False}
            )
            # Queue three distinct replies at the configured default limit. No real sends.
            for number in range(3):
                mock.append("LB26_E2E_" + f"{number:024X}")
                await ingest_new(runtime)
                assert (
                    await runtime.pipeline.process_once(CHANNEL)
                ).status is PipelineStatus.COMPLETED
        mock.append(COMMAND)
        await ingest_new(runtime)
        result = await runtime.pipeline.process_once(CHANNEL)
        assert result.status is PipelineStatus.SKIPPED
        assert probe.requests == []
        assert mock.posts == 0
    finally:
        await runtime.close()


async def test_runtime_entrypoint_respects_the_lab_transport_lock(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lemonbot.runtime import run_service

    runtime = await setup_runtime(tmp_path, monkeypatch, SimulatedWechat())
    settings = runtime.settings
    await runtime.close()
    with RuntimeLock(Path(settings.wechat_agent.state_dir) / "run.lock", persistent=True):
        with pytest.raises(AlreadyRunningError):
            await run_service(settings)


async def test_control_resume_rechecks_account_and_emergency_blocks_recovery(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    from lemonbot.runtime import RepositoryControl

    mock = SimulatedWechat()
    runtime = await setup_runtime(tmp_path, monkeypatch, mock)
    try:
        assert isinstance(runtime.connector, RuntimeAgentWechatConnector)
        assert runtime._policy is not None
        control = RepositoryControl(
            runtime.repository,
            profile="lab",
            connector_name="wechat_agent",
            started_at=datetime.now(UTC),
            emergency_event=runtime._emergency,
            approvals=runtime.approval_service,
            tools={},
            policy=runtime._policy,
            granted_tool_scopes=frozenset(),
            side_effect_lock=runtime._side_effect_lock,
            emergency_file=runtime.paths.emergency_stop_file,
            agent_connector=runtime.connector,
        )
        status = await control.set_pause(CHANNEL, True)
        assert status.channel_pauses[CHANNEL]
        assert status.channel_pause_reasons[CHANNEL] == "ManualPause"
        original_account = mock.account
        mock.account = "different-synthetic-account"
        with pytest.raises(RuntimeError, match="verification failed"):
            await control.set_pause(CHANNEL, False)
        status = await control.status()
        assert status.channel_pauses[CHANNEL]
        assert status.channel_pause_reasons[CHANNEL] == "AccountChanged"
        mock.account = original_account
        assert not (await control.set_pause(CHANNEL, False)).channel_pauses[CHANNEL]
        await control.emergency_stop()
        assert runtime.paths.emergency_stop_file.exists()
        with pytest.raises(RuntimeError, match="restart is required"):
            await control.set_pause(None, False)
        assert mock.posts == 0
    finally:
        await runtime.close()


async def test_management_bootstrap_is_private_and_not_logged(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
    capsys: pytest.CaptureFixture[str],
) -> None:
    import stat

    from lemonbot.connectors.wechat_agent_state import private_read

    class ImmediateServer:
        should_exit = False

        async def serve(self) -> None:
            return

    runtime = await setup_runtime(tmp_path, monkeypatch, SimulatedWechat())
    monkeypatch.setattr("lemonbot.runtime.uvicorn.Server", lambda config: ImmediateServer())
    try:
        await runtime.serve()
        path = runtime.paths.lock_file.parent / "lab.admin-url"
        url = private_read(path).decode()
        assert url.startswith("http://127.0.0.1:8765/login#")
        assert stat.S_IMODE(path.stat().st_mode) == 0o600
        assert url.split("#", 1)[1] not in capsys.readouterr().out
    finally:
        await runtime.close()


async def test_runtime_preserves_pause_code_written_by_independent_lab(
    tmp_path: Path,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    mock = SimulatedWechat()
    runtime = await setup_runtime(tmp_path, monkeypatch, mock)
    await runtime.close()
    private_write(tmp_path / "binding" / "paused.json", b'{"code":"TestingNotForeground"}')
    runtime = await setup_runtime(tmp_path, monkeypatch, mock)
    try:
        assert isinstance(runtime.connector, RuntimeAgentWechatConnector)
        assert runtime.connector.pause_reason == "TestingNotForeground"
        assert not runtime.connector.ready
        assert await runtime.repository.is_paused(CHANNEL)
        assert mock.posts == 0
    finally:
        await runtime.close()


async def test_busy_masks_new_reads_and_keeps_pause_sticky(
    tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    mock = SimulatedWechat()
    runtime = await setup_runtime(tmp_path, monkeypatch, mock)
    assert isinstance(runtime.connector, RuntimeAgentWechatConnector)
    connector = runtime.connector
    connector.transport.poll_seconds = 0.01
    stream = connector.events()
    pending = None
    try:
        async with connector.processing_operation():
            mock.append(COMMAND)
            pending = asyncio.create_task(anext(stream))
            await asyncio.sleep(0.05)
            assert not pending.done()
            assert connector.transport.anchors == set()
            # Busy cannot accidentally become enabled through a resume.
            with pytest.raises(RuntimeError, match="busy"):
                await connector.resume()
        assert (await asyncio.wait_for(pending, 1)).text == COMMAND
        await connector.pause()
        async with connector.processing_operation():
            mock.append("LB26_E2E_" + "E" * 24)
        assert not connector.ready
        assert connector.pause_reason == "ManualPause"
    finally:
        if pending is not None:
            pending.cancel()
            await asyncio.gather(pending, return_exceptions=True)
        await stream.aclose()
        await runtime.close()

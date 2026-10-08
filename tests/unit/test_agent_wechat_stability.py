from __future__ import annotations

from pathlib import Path
from typing import Any
from unittest.mock import AsyncMock, Mock

import pytest

from lemonbot.connectors.wechat_agent import CHANNEL, LabPaused
from lemonbot.connectors.wechat_agent_state import private_write
from lemonbot.domain.models import OutboundMessage
from lemonbot.research.agent_wechat_stability import observe, snapshot
from lemonbot.storage import CoreRepository, Database


async def test_observer_requires_elapsed_window_and_samples_stable_state(tmp_path: Path) -> None:
    path = tmp_path / "lab.db"
    db = Database.from_path(path)
    await db.initialise()
    connector = Mock(guard=AsyncMock())
    checkpoints: list[dict[str, Any]] = []
    try:
        result = await observe(
            connector,
            path,
            pause_file=tmp_path / "paused.json",
            seconds=0.05,
            interval=0.01,
            active=lambda: True,
            checkpoint=lambda value: checkpoints.append(dict(value)),
        )
        assert result["passed"]
        assert result["elapsed_seconds"] >= result["required_seconds"]
        assert result["samples"] >= 2
        assert result["baseline"] == result["last_snapshot"]
        assert checkpoints[-1]["passed"]
        assert not connector.deliver.called
    finally:
        await db.close()


@pytest.mark.parametrize("failure", ["file_pause", "durable_pause", "inactive", "guard", "pending"])
async def test_active_process_alone_cannot_pass_stability_observation(
    tmp_path: Path,
    failure: str,
) -> None:
    path = tmp_path / "lab.db"
    db = Database.from_path(path)
    await db.initialise()
    repo = CoreRepository(db)
    connector = Mock(guard=AsyncMock())
    try:
        pause_file = tmp_path / "paused.json"
        if failure == "file_pause":
            private_write(pause_file, b'{"reason":"ManualPause"}')
        elif failure == "durable_pause":
            await repo.set_paused(channel=CHANNEL, paused=True)
        elif failure == "guard":
            connector.guard.side_effect = LabPaused("TestingNotForeground")
        elif failure == "pending":
            await repo.create_outbox(
                OutboundMessage(
                    channel=CHANNEL, chat_id="synthetic-chat", text="LB26_ACK_" + "A" * 24
                )
            )
        result = await observe(
            connector,
            path,
            pause_file=pause_file,
            seconds=0,
            active=lambda: failure != "inactive",
            checkpoint=lambda value: None,
        )
        assert not result["passed"]
        assert result["failure_code"] in {
            "ObservedChannelPaused",
            "ObservedServiceNotActive",
            "TestingNotForeground",
            "ObservedUnsettledOutbox",
        }
        assert not connector.deliver.called
    finally:
        await db.close()


async def test_observer_rejects_database_activity_during_window(tmp_path: Path) -> None:
    path = tmp_path / "lab.db"
    db = Database.from_path(path)
    await db.initialise()
    repo = CoreRepository(db)
    connector = Mock(guard=AsyncMock())
    checks = 0

    async def guard() -> None:
        nonlocal checks
        checks += 1
        if checks == 2:
            await repo.create_outbox(
                OutboundMessage(
                    channel=CHANNEL, chat_id="synthetic-chat", text="LB26_ACK_" + "A" * 24
                )
            )

    connector.guard.side_effect = guard
    try:
        result = await observe(
            connector,
            path,
            pause_file=tmp_path / "paused.json",
            seconds=1,
            interval=0.01,
            active=lambda: True,
            checkpoint=lambda value: None,
        )
        assert not result["passed"]
        assert result["failure_code"] == "ObservationSnapshotChanged"
        assert snapshot(path)["model_io_started_count"] == 0
        assert not connector.deliver.called
    finally:
        await db.close()

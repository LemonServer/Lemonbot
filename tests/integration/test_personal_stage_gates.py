from __future__ import annotations

import asyncio

import pytest

from lemonbot.config import AppSettings
from lemonbot.domain import InboundEvent
from lemonbot.orchestration import (
    EventPipeline,
    FakeConnector,
    FakeModelBackend,
    PipelineConfig,
    PipelineStatus,
)
from lemonbot.policy import DeterministicPolicy
from lemonbot.runtime import _pipeline_output_mode
from lemonbot.storage import CoreRepository, Database


def run(coroutine):
    return asyncio.run(coroutine)


async def make_pipeline(tmp_path, output_mode: str):
    database = Database.from_path(tmp_path / f"{output_mode}.db")
    await database.initialise()
    repository = CoreRepository(database)
    await repository.set_allowlisted("fake", "chat-1")
    model = FakeModelBackend(["我是 AI，这是建议回复。"])
    pipeline = EventPipeline(
        repository,
        DeterministicPolicy(repository),
        model,
        config=PipelineConfig(output_mode=output_mode),
    )
    event = InboundEvent(
        channel="fake",
        event_id=f"{output_mode}-event",
        chat_id="chat-1",
        sender_id="user-1",
        text="你好",
    )
    return database, repository, model, pipeline, event


def test_observe_records_event_without_model_draft_or_outbox(tmp_path) -> None:
    async def scenario() -> None:
        database, repository, model, pipeline, event = await make_pipeline(tmp_path, "observe")
        connector = FakeConnector(channel="fake")
        try:
            await pipeline.ingest(event)
            result = await pipeline.process_once("fake")

            assert result.status is PipelineStatus.COMPLETED
            assert model.requests == []
            assert await repository.pending_drafts(channel="fake") == []
            assert await repository.reserve_next_outbox("fake") is None
            assert (
                await pipeline.dispatch_once(connector, channel="fake")
            ).status is PipelineStatus.IDLE
            assert connector.delivered == []
        finally:
            await database.close()

    run(scenario())


def test_draft_calls_model_once_but_never_creates_or_dispatches_outbox(tmp_path) -> None:
    async def scenario() -> None:
        database, repository, model, pipeline, event = await make_pipeline(tmp_path, "draft")
        connector = FakeConnector(channel="fake")
        try:
            await pipeline.ingest(event)
            result = await pipeline.process_once("fake")

            assert result.status is PipelineStatus.COMPLETED
            assert len(model.requests) == 1
            drafts = await repository.pending_drafts(channel="fake", chat_id="chat-1")
            assert len(drafts) == 1
            assert drafts[0].reply_to_event_id == event.event_id
            assert drafts[0].text == "我是 AI，这是建议回复。"
            assert drafts[0].metadata_json == {"proactive": False, "draft": True}
            assert await repository.reserve_next_outbox("fake") is None
            assert (
                await pipeline.dispatch_once(connector, channel="fake")
            ).status is PipelineStatus.IDLE
            assert connector.delivered == []
        finally:
            await database.close()

    run(scenario())


@pytest.mark.parametrize(
    ("stage", "expected"),
    [
        ("observe", "observe"),
        ("draft", "draft"),
        ("reply", "send"),
        ("proactive", "send"),
    ],
)
def test_runtime_maps_personal_wechat_stage_to_pipeline_mode(stage, expected) -> None:
    raw = AppSettings().model_dump(mode="python")
    raw["profile"] = "lab"
    raw["runtime"]["connector"] = "wechat_uia"
    raw["wechat_uia"].update(
        {
            "enabled": True,
            "stage": stage,
            "expected_account": "account-sha256",
            "expected_windows_user": "lab-user",
            "expected_executable_path": r"C:\Program Files\Tencent\WeChat\WeChat.exe",
            "expected_executable_sha256": "c" * 64,
            "enrolled_client_version": "4.0.0",
            "enrolled_selector_signature": "selector-sha256",
            "selector_bundle_path": "selectors.json",
            "allow_chat_ids": ["chat-1"],
        }
    )
    settings = AppSettings.model_validate(raw)

    assert _pipeline_output_mode(settings) == expected


def test_runtime_non_uia_connectors_always_send() -> None:
    assert _pipeline_output_mode(AppSettings()) == "send"

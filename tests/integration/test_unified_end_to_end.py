"""Composed synthetic metadata -> policy -> retrieval -> model -> unique HTTP receipt."""

from __future__ import annotations

import asyncio
import importlib.util
import json
from pathlib import Path

import pytest
from test_context_retrieval import SyntheticEmbedder
from test_two_stage_context import system as system

from lemonbot.connectors.wechat_agent import CHANNEL
from lemonbot.connectors.wechat_agent_runtime import RuntimeAgentWechatConnector
from lemonbot.domain import ModelResponse
from lemonbot.memory.unified.models import ContextDecision
from lemonbot.memory.unified.wechat import archive_scope, snapshot_message
from lemonbot.orchestration import PipelineStatus
from lemonbot.policy import DeterministicPolicy, PolicyConfig

# Reuse the existing synthetic transport without changing pytest's global import paths.
_spec = importlib.util.spec_from_file_location(
    "stage7_synthetic_transport", Path(__file__).parents[1] / "unit/test_wechat_conversation.py"
)
assert _spec is not None and _spec.loader is not None
_fixture = importlib.util.module_from_spec(_spec)
_spec.loader.exec_module(_fixture)
KEY, TwoChats, setup = _fixture.KEY, _fixture.TwoChats, _fixture.setup


async def graph(system, mock, state):
    transport = await setup(mock, state)
    flow = system["flow"]
    from lemonbot.memory.unified.maintenance import UnifiedMemoryStore
    from lemonbot.memory.unified.remember import MemoryContextRetriever

    store = UnifiedMemoryStore(system["archive"])
    flow.memories = store
    flow.assembler.memories = store
    flow.retriever = MemoryContextRetriever(system["retriever"], store)
    flow.scopes = {
        ref: archive_scope(child.binding, is_group=child.target.is_group)
        for ref, child in transport.children.items()
    }
    for ref, child in transport.children.items():
        child.archive = system["archive"]
        await system["repository"].set_allowlisted(CHANNEL, ref)

    def observed(outbound):
        child = transport.children[outbound.chat_id]
        row = child.last_confirmed_row
        if row is None or row[0] != str(outbound.message_id):
            return None
        return snapshot_message(
            row[1], binding=child.binding, key=KEY, is_group=child.target.is_group
        )

    flow.confirmed_source = observed
    system["pipeline"].policy = DeterministicPolicy(
        system["repository"], config=PolicyConfig(quiet_enabled=False)
    )
    wrapper = RuntimeAgentWechatConnector(transport, system["repository"], state)
    await wrapper.initialize()
    return transport, wrapper


async def arrivals(transport):
    await asyncio.wait_for(transport.wait_for_changes(), 2)
    return await transport.poll()


@pytest.mark.parametrize("gpu_failure", [False, True])
@pytest.mark.parametrize("unknown", [False, True])
async def test_composed_group_sharing_private_isolation_restart_and_send(
    system, tmp_path, gpu_failure, unknown
):
    mock = TwoChats()
    transport, wrapper = await graph(system, mock, tmp_path)
    model = system["model"]
    pipeline = system["pipeline"]
    base = system["retriever"]
    gpu = SyntheticEmbedder(fail="exit" if gpu_failure else "")
    base.worker = gpu

    def classify(request):
        body = json.loads(request.messages[-1].content)
        current = body["current"]
        # The compact prompt omits the duplicate candidate.scope; every complete
        # SourceRef still carries and must match the exact current scope.
        assert all(
            c["sources"] and all(ref["scope"] == current["scope"] for ref in c["sources"])
            for c in body["candidates"]
        )
        if current["scope"]["kind"] == "private":
            assert "GROUPWORD" not in json.dumps(body["candidates"])
            return ContextDecision(mode="standalone", relation="none").model_dump_json()
        assert "PRIVATEWORD" not in json.dumps(body["candidates"])
        chosen = next(c for c in body["candidates"] if "GROUPWORD" in c["summary"])
        assert chosen["author_ids"] != [current["sender_id"]]
        return ContextDecision(
            mode="related", relation="followup", selected_candidate_ids=(chosen["candidate_id"],)
        ).model_dump_json()

    model.classify = classify
    model.answer = lambda request: ModelResponse(
        content="safe synthetic answer", model="deepseek-flash"
    )
    try:
        ordinary = mock.add(mock.group, "群短词 GROUPWORD")
        ordinary["sender"] = "synthetic-alice"
        mock.add(mock.group, "@self 手打文本")
        mock.add(mock.group, "@all 全体")
        quote = mock.add(mock.group, "引用中 @self")
        quote.update({"type": 49, "quoteReply": True, "quotedText": "@self only in quote"})
        events = await arrivals(transport)
        assert events == [] and model.requests == [] and mock.posts == 0
        group = next(s for s in system["flow"].scopes.values() if s.kind == "group")
        private = next(s for s in system["flow"].scopes.values() if s.kind == "private")
        rows = await system["archive"].list_messages(scope=group, before_message_id=None, limit=20)
        assert len(rows) == 4
        seed = rows[0]
        for i, other in enumerate(
            (
                group.model_copy(update={"account_id": "another-account"}),
                group.model_copy(update={"conversation_id": "another-group"}),
                private.model_copy(update={"conversation_id": "another-private"}),
            )
        ):
            await system["archive"].put_message(
                seed.model_copy(
                    update={"scope": other, "message_id": f"noise-{i}", "event_id": f"noise-{i}"}
                )
            )
        bob = mock.add(mock.group, "群短词是什么？", mention=True)
        bob["sender"] = "synthetic-bob"
        events = await arrivals(transport)
        assert len(events) == 1
        await pipeline.ingest(events[0])
        async with wrapper.processing_operation():
            assert (await pipeline.process_once(CHANNEL)).status is PipelineStatus.COMPLETED
            assert not wrapper.processing_idle.is_set()
        assert (
            await pipeline.dispatch_once(wrapper, channel=CHANNEL)
        ).status is PipelineStatus.ACKNOWLEDGED
        assert mock.posts == 1 and len(model.requests) == 2
        assert (base.worker is None) is gpu_failure
        assert bool(base.last_sync.degraded_reason) is gpu_failure
        # A real model is never invoked by these diagnostics or by ordinary group archival.
        mock.add(mock.peer, "私聊短词 PRIVATEWORD")
        events = await arrivals(transport)
        assert len(events) == 1
        await pipeline.ingest(events[0])
        async with wrapper.processing_operation():
            assert (await pipeline.process_once(CHANNEL)).status is PipelineStatus.COMPLETED
        assert (
            await pipeline.dispatch_once(wrapper, channel=CHANNEL)
        ).status is PipelineStatus.ACKNOWLEDGED
        assert mock.posts == 2 and len(model.requests) == 4
        await wrapper.pause("ManualPause")
        await wrapper.close()
        downtime = mock.add(mock.group, "downtime should never replay", mention=True)
        transport, wrapper = await graph(system, mock, tmp_path)
        assert not wrapper.ready and await system["repository"].is_paused(CHANNEL)
        assert mock.posts == 2 and len(model.requests) == 4
        await wrapper.resume()
        assert wrapper.ready
        bob = mock.add(mock.group, "再次问群短词？", mention=True)
        bob["sender"] = "synthetic-bob"
        events = await arrivals(transport)
        assert len(events) == 1 and "downtime" not in events[0].text
        await pipeline.ingest(events[0])
        async with wrapper.processing_operation():
            assert (await pipeline.process_once(CHANNEL)).status is PipelineStatus.COMPLETED
        mock.duplicate_reply = unknown
        result = await pipeline.dispatch_once(wrapper, channel=CHANNEL)
        assert result.status is (PipelineStatus.UNKNOWN if unknown else PipelineStatus.ACKNOWLEDGED)
        assert mock.posts == 3 and len(model.requests) == 6
        assert (
            await pipeline.dispatch_once(wrapper, channel=CHANNEL)
        ).status is PipelineStatus.IDLE
        assert mock.posts == 3 and len(model.requests) == 6
        messages = await system["archive"].list_messages(
            scope=group, before_message_id=None, limit=40
        )
        assert all(m.transport_message_id != str(downtime["localId"]) for m in messages)
        assert sum(m.direction == "outbound" for m in messages) == (1 if unknown else 2)
        if unknown:
            assert not wrapper.ready and await system["repository"].is_paused(CHANNEL)
            await wrapper.close()
            transport, wrapper = await graph(system, mock, tmp_path)
            assert not wrapper.ready
            assert (
                await pipeline.dispatch_once(wrapper, channel=CHANNEL)
            ).status is PipelineStatus.IDLE
            assert mock.posts == 3 and len(model.requests) == 6
    finally:
        await wrapper.close()

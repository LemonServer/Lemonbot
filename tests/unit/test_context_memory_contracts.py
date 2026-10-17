from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta
from pathlib import Path
from typing import Any

import pytest
from pydantic import ValidationError

from lemonbot.config.settings import AppSettings, load_settings
from lemonbot.domain.models import OutboxState
from lemonbot.memory.models import ContextBundle as LegacyContextBundle
from lemonbot.memory.unified.models import (
    AttachmentRef,
    ContextBundle,
    ContextDecision,
    ConversationScope,
    EmbeddingRequest,
    EmbeddingResult,
    MemoryEntry,
    Message,
    MessagePart,
    Observation,
    RetrievalCandidate,
    RetrievalResult,
    SourceRef,
)

NOW = datetime(2026, 10, 15, tzinfo=UTC)
GROUP = ConversationScope(
    channel="synthetic", account_id="account-a", conversation_id="group-a", kind="group"
)


def message(identifier: str = "m1", **changes: Any) -> Message:
    return Message.model_validate(
        {
            "scope": GROUP,
            "message_id": identifier,
            "sender_id": "alice",
            "direction": "inbound",
            "occurred_at": NOW,
            "parts": (MessagePart(part_id="text", kind="text", text="合成测试文字"),),
            **changes,
        }
    )


def source(identifier: str = "m1", **changes: Any) -> SourceRef:
    return SourceRef.model_validate({"scope": GROUP, "message_id": identifier, **changes})


def decision(**changes: Any) -> ContextDecision:
    return ContextDecision.model_validate({"mode": "standalone", "relation": "none", **changes})


def test_disabled_configuration_is_backward_compatible_and_cuda_first(tmp_path: Path) -> None:
    omitted = tmp_path / "omitted.toml"
    explicit = tmp_path / "explicit.toml"
    omitted.write_text('profile="lab"\n', encoding="utf-8")
    explicit.write_text(
        'profile="lab"\n[context_memory]\nenabled=false\nembedding_backend="tensorrt"\n',
        encoding="utf-8",
    )
    assert load_settings(omitted) == load_settings(explicit)
    assert AppSettings().context_memory.embedding_backend == "tensorrt"
    assert not AppSettings().context_memory.enabled
    cpu = AppSettings.model_validate({"context_memory": {"embedding_backend": "cpu"}})
    assert cpu.context_memory.embedding_backend == "cpu"
    with pytest.raises(ValidationError, match="unified context requires archive"):
        AppSettings.model_validate({"context_memory": {"enabled": True}})
    with pytest.raises(ValidationError):
        AppSettings.model_validate({"context_memory": {"embedding_backend": "auto"}})
    example = load_settings(Path("config/wechat-agent-conversation.example.toml"))
    assert not example.context_memory.enabled
    assert not example.wechat_agent.enabled


def test_uniform_parts_preserve_raw_content_and_json_roundtrip() -> None:
    attachment = AttachmentRef(
        sha256="a" * 64, size=123, media_type="image/png", expires_at=NOW + timedelta(days=7)
    )
    original = message(
        parts=(
            MessagePart(part_id="text", kind="text", text="原始问题，不混入引用文字"),
            MessagePart(part_id="image", kind="image", attachment=attachment),
            MessagePart(part_id="url", kind="link", url="https://example.org/"),
            MessagePart(part_id="card", kind="card", url="https://example.org/card"),
            MessagePart(part_id="future", kind="unknown", unavailable_reason="unsupported_kind"),
        ),
        references=(source("older", part_id="image"),),
    )
    assert Message.model_validate_json(original.model_dump_json()) == original
    assert original.parts[0].text == "原始问题，不混入引用文字"
    assert original.references[0].message_id == "older"
    assert ContextBundle is not LegacyContextBundle
    assert message("same-body-1").message_id != message("same-body-2").message_id


@pytest.mark.parametrize(
    "raw",
    [
        {"part_id": "p", "kind": "text"},
        {"part_id": "p", "kind": "image"},
        {"part_id": "p", "kind": "card"},
        {"part_id": "p", "kind": "unknown"},
        {"part_id": "p", "kind": "image", "base64": "not-accepted"},
        {"part_id": "p", "kind": "image", "path": "/untrusted/not-authorized"},
    ],
)
def test_parts_cannot_hide_missing_content_or_inline_bytes(raw: dict[str, object]) -> None:
    with pytest.raises(ValidationError):
        MessagePart.model_validate(raw)


def test_group_scope_preserves_distinct_authors_and_private_account_boundaries() -> None:
    alice = message("alice-msg")
    bob = message("bob-msg", sender_id="bob")
    assert alice.scope == bob.scope and alice.sender_id != bob.sender_id
    for changed in (
        {"conversation_id": "group-b"},
        {"account_id": "account-b"},
        {"kind": "private"},
        {"channel": "other-channel"},
    ):
        foreign = ConversationScope.model_validate({**GROUP.model_dump(), **changed})
        with pytest.raises(ValidationError, match="cannot cross conversation scope"):
            message(references=(source(scope=foreign),))
        with pytest.raises(ValidationError, match="cannot cross conversation scope"):
            ContextBundle(
                current=alice,
                decision=decision(mode="related", selected_candidate_ids=("candidate",)),
                source_messages=(message("foreign", scope=foreign),),
            )


@pytest.mark.parametrize("state", list(OutboxState))
def test_only_acknowledged_assistant_messages_can_become_context(state: OutboxState) -> None:
    assistant = message("outbound", direction="outbound", delivery_state=state)
    assert assistant.eligible_as_evidence == (state is OutboxState.ACKNOWLEDGED)
    kwargs = dict(
        current=message(),
        decision=decision(mode="related", selected_candidate_ids=("candidate",)),
        source_messages=(assistant,),
    )
    if state is OutboxState.ACKNOWLEDGED:
        ContextBundle(**kwargs)
    else:
        with pytest.raises(ValidationError, match="unconfirmed assistant"):
            ContextBundle(**kwargs)


def test_archive_requires_explicit_direction_and_unique_parts() -> None:
    with pytest.raises(ValidationError, match="explicit delivery state"):
        message(direction="outbound")
    with pytest.raises(ValidationError, match="explicit delivery state"):
        message(delivery_state=OutboxState.ACKNOWLEDGED)
    with pytest.raises(ValidationError, match="part IDs must be unique"):
        message(parts=(message().parts[0], message().parts[0]))
    with pytest.raises(ValidationError, match="timezone"):
        message(occurred_at=datetime(2026, 10, 15))


def test_observations_keep_reading_failure_distinct_from_content() -> None:
    fields = {
        "observation_id": "observation-1",
        "source": source(part_id="link"),
        "extractor_version": "reader/v1",
        "created_at": NOW,
    }
    failure = Observation(
        **fields, kind="page_body", status="unavailable", error_code="verification_required"
    )
    assert failure.text is None
    with pytest.raises(ValidationError, match="not extracted content"):
        Observation(**fields, kind="page_body", status="unavailable", text="fake body")
    with pytest.raises(ValidationError, match="require extracted content"):
        Observation(**fields, kind="unavailable", status="available", text="fake body")


def test_memory_requires_attributed_scoped_provenance() -> None:
    fields = {
        "memory_id": "memory-1",
        "scope": GROUP,
        "kind": "fact",
        "text": "Alice 说活动暗号是银杏。",
        "sources": (source(),),
        "author_ids": ("alice",),
        "derivation_version": "synthetic/v1",
        "created_at": NOW,
    }
    memory = MemoryEntry.model_validate(fields)
    assert memory.sources[0].message_id == "m1"
    with pytest.raises(ValidationError):
        MemoryEntry.model_validate({**fields, "sources": ()})
    with pytest.raises(ValidationError, match="supersede itself"):
        MemoryEntry.model_validate({**fields, "supersedes": ("memory-1",)})
    with pytest.raises(ValidationError, match="active and in scope"):
        ContextBundle(
            current=message(),
            decision=decision(mode="related", selected_candidate_ids=("candidate",)),
            memories=(MemoryEntry.model_validate({**fields, "active": False}),),
        )


def test_classifier_contract_is_bounded_and_cannot_rewrite_the_user_message() -> None:
    assert decision().selected_candidate_ids == ()
    with pytest.raises(ValidationError):
        decision(rewritten_message="model-authored substitute")
    with pytest.raises(ValidationError, match="require selected evidence"):
        decision(mode="related")
    with pytest.raises(ValidationError, match="only related"):
        decision(selected_candidate_ids=("candidate",))
    with pytest.raises(ValidationError):
        decision(mode="related", selected_candidate_ids=tuple(f"c{i}" for i in range(7)))
    with pytest.raises(ValidationError, match="exact source"):
        decision(needs_original_image=True)
    with pytest.raises(ValidationError, match="exact part ID"):
        decision(needs_original_image=True, image_source=source())
    with pytest.raises(ValidationError, match="cannot contain history"):
        ContextBundle(current=message(), decision=decision(), source_messages=(message("old"),))
    with pytest.raises(ValidationError, match="historical image"):
        ContextBundle(
            current=message(),
            decision=decision(needs_original_image=True, image_source=source("old", part_id="p")),
        )


def test_retrieval_is_bounded_scoped_and_explicit_about_degradation() -> None:
    candidate = RetrievalCandidate(
        candidate_id="candidate", scope=GROUP, sources=(source(),), summary="合成候选",
        author_ids=("alice",), reasons=("keyword",), score=0.5,
    )
    result = RetrievalResult(
        scope=GROUP, candidates=(candidate,), index_version="v1", degraded_reason="gpu_unavailable"
    )
    assert result.degraded_reason == "gpu_unavailable"
    with pytest.raises(ValidationError, match="unique"):
        RetrievalResult(scope=GROUP, candidates=(candidate, candidate), index_version="v1")
    with pytest.raises(ValidationError):
        RetrievalResult(scope=GROUP, candidates=(candidate,) * 21, index_version="v1")
    with pytest.raises(ValidationError, match="scope"):
        RetrievalResult(
            scope=ConversationScope.model_validate({**GROUP.model_dump(), "account_id": "other"}),
            candidates=(candidate,), index_version="v1",
        )


def test_embedding_contract_bounds_ipc_and_records_actual_backend() -> None:
    request = EmbeddingRequest(request_id="r1", purpose="query", texts=("合成查询",))
    result = EmbeddingResult(
        request_id=request.request_id, model_version="bge-test/v1", backend="tensorrt",
        precision="fp16", vectors=((1.0,) + (0.0,) * 511,),
    )
    assert len(result.vectors[0]) == 512
    with pytest.raises(ValidationError):
        EmbeddingRequest(request_id="r2", purpose="query", texts=("x",) * 17)
    with pytest.raises(ValidationError):
        EmbeddingRequest(request_id="r3", purpose="query", texts=("x" * 4001,))
    with pytest.raises(ValidationError):
        EmbeddingResult.model_validate({**result.model_dump(), "vectors": ((float("nan"),) * 512,)})
    with pytest.raises(ValidationError):
        EmbeddingResult.model_validate({**result.model_dump(), "vectors": ((1.0,),)})


def test_synthetic_scenario_oracles_have_valid_sources_and_keep_authors() -> None:
    path = Path(__file__).parents[1] / "fixtures/context_memory/scenarios.json"
    raw = json.loads(path.read_text(encoding="utf-8"))
    messages: dict[str, Message] = {}
    for item in raw["messages"]:
        scope = ConversationScope(
            channel="synthetic", account_id="account-a",
            conversation_id=item["chat"], kind=item["kind"],
        )
        refs = (
            (SourceRef(scope=scope, message_id=item["quote"], part_id=item["quote_part"]),)
            if "quote" in item else ()
        )
        messages[item["id"]] = message(
            item["id"], scope=scope, sender_id=item["sender"], parts=item["parts"], references=refs
        )
    for case in raw["cases"]:
        current = messages[case["current"]]
        for identifier in case["expected_sources"]:
            assert messages[identifier].scope == current.scope
            assert identifier not in case["excluded_sources"]
        assert set(case["excluded_sources"]) <= messages.keys()
    assert messages["group-word"].scope == messages["group-followup"].scope
    assert messages["group-word"].sender_id != messages["group-followup"].sender_id
    quote = messages["explicit-image-a"].references[0]
    assert quote.message_id == "image-a" and quote.part_id == "image"

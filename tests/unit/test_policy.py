from __future__ import annotations

import asyncio
from datetime import UTC, datetime

import pytest

from lemonbot.domain import PolicyDecision, ProposedAction
from lemonbot.policy import DeterministicPolicy
from lemonbot.policy.config import PolicyConfig, RateLimitProfile
from lemonbot.storage import CoreRepository, Database


def run(coroutine):
    return asyncio.run(coroutine)


async def make_policy(*, now: datetime | None = None):
    database = Database.in_memory()
    await database.initialise()
    repository = CoreRepository(database)
    policy = DeterministicPolicy(
        repository,
        clock=(lambda: now) if now is not None else None,
    )
    return database, repository, policy


@pytest.mark.parametrize(
    "kind",
    [
        "purchase",
        "transfer_money",
        "credential_access",
        "install_software",
        "elevate",
        "permanent_delete",
        "shell",
        "arbitrary_code",
        "batch_send",
        "add_contact",
        "mention_all",
    ],
)
def test_hard_denials_cannot_be_enrolled(kind: str) -> None:
    async def scenario() -> None:
        database, repository, policy = await make_policy()
        try:
            await repository.set_allowlisted("wecom", "chat-1")
            result = await policy.evaluate(
                ProposedAction(
                    kind=kind,
                    channel="wecom",
                    chat_id="chat-1",
                    bound_channel="wecom",
                    bound_chat_id="chat-1",
                    side_effect=True,
                    arguments={"enrolled": True, "approved": True},
                )
            )
            assert result.decision is PolicyDecision.DENY
            assert result.rule_id == "hard_denial"
        finally:
            await database.close()

    run(scenario())


def test_unknown_action_is_denied_by_default() -> None:
    async def scenario() -> None:
        database, _, policy = await make_policy()
        try:
            result = await policy.evaluate(ProposedAction(kind="invented_capability"))
            assert result.decision is PolicyDecision.DENY
            assert result.rule_id == "unknown_action"
        finally:
            await database.close()

    run(scenario())


def test_runtime_personal_channel_uses_configured_lab_limits() -> None:
    custom = RateLimitProfile(
        reply_per_10_minutes=1,
        reply_per_hour=2,
        reply_per_day=3,
        global_per_day=4,
        proactive_cooldown_hours=5,
        proactive_per_day=6,
        proactive_global_per_day=7,
        proactive_enabled=True,
    )
    config = PolicyConfig(wechat_lab=custom)

    assert config.limits_for("wechat_personal_lab") == custom
    assert config.limits_for("wechat_personal_lab").proactive_enabled


def test_reply_requires_allowlist_and_broker_owned_destination() -> None:
    async def scenario() -> None:
        database, repository, policy = await make_policy()
        try:
            action = ProposedAction(
                kind="reply",
                channel="wecom",
                chat_id="chat-1",
                bound_channel="wecom",
                bound_chat_id="chat-1",
                reason_event_id="event-1",
                side_effect=True,
            )
            assert (await policy.evaluate(action)).decision is PolicyDecision.ENROLL

            await repository.set_allowlisted("wecom", "chat-1")
            assert (await policy.evaluate(action)).decision is PolicyDecision.AUTO

            redirected = action.model_copy(update={"chat_id": "attacker-chat"})
            result = await policy.evaluate(redirected)
            assert result.decision is PolicyDecision.DENY
            assert result.rule_id == "destination_mismatch"
        finally:
            await database.close()

    run(scenario())


def test_pause_blocks_side_effect_before_commit() -> None:
    async def scenario() -> None:
        database, repository, policy = await make_policy()
        try:
            await repository.set_allowlisted("wecom", "chat-1")
            await repository.set_paused(channel="wecom", paused=True)
            result = await policy.evaluate(
                ProposedAction(
                    kind="reply",
                    channel="wecom",
                    chat_id="chat-1",
                    bound_channel="wecom",
                    bound_chat_id="chat-1",
                    reason_event_id="event-1",
                    side_effect=True,
                )
            )
            assert result.decision is PolicyDecision.DENY
            assert result.rule_id == "runtime_paused"
        finally:
            await database.close()

    run(scenario())


def test_proactive_message_requires_reason_and_respects_quiet_hours() -> None:
    # 23:30 Asia/Shanghai is 15:30 UTC.
    now = datetime(2026, 8, 16, 15, 30, tzinfo=UTC)

    async def scenario() -> None:
        database, repository, policy = await make_policy(now=now)
        try:
            await repository.set_allowlisted("wecom", "chat-1")
            base = ProposedAction(
                kind="proactive_message",
                channel="wecom",
                chat_id="chat-1",
                proactive=True,
                side_effect=True,
            )
            missing_reason = await policy.evaluate(base)
            assert missing_reason.rule_id == "proactive_no_reason"

            quiet = await policy.evaluate(
                base.model_copy(update={"reason_event_id": "commitment-1"})
            )
            assert quiet.decision is PolicyDecision.DENY
            assert quiet.rule_id == "quiet_hours"
        finally:
            await database.close()

    run(scenario())


def test_secret_data_cannot_be_sent_to_model() -> None:
    async def scenario() -> None:
        database, _, policy = await make_policy()
        try:
            result = await policy.evaluate(
                ProposedAction(kind="model_call", arguments={"data_class": "SECRET"})
            )
            assert result.decision is PolicyDecision.DENY
            assert result.rule_id == "secret_boundary"
        finally:
            await database.close()

    run(scenario())


@pytest.mark.parametrize("kind", ["mcp_read", "mcp_write"])
def test_secret_data_cannot_be_passed_to_mcp(kind: str) -> None:
    async def scenario() -> None:
        database, _, policy = await make_policy()
        try:
            result = await policy.evaluate(
                ProposedAction(kind=kind, arguments={"data_class": "SECRET"})
            )
            assert result.decision is PolicyDecision.DENY
            assert result.rule_id == "secret_boundary"
        finally:
            await database.close()

    run(scenario())

"""Deterministic, fail-closed policy evaluation."""

from __future__ import annotations

from collections.abc import Callable
from datetime import UTC, datetime, timedelta
from uuid import UUID
from zoneinfo import ZoneInfo

from lemonbot.domain import (
    AuditRecord,
    DataClass,
    PolicyDecision,
    PolicyEvaluation,
    ProposedAction,
)
from lemonbot.storage import CoreRepository

from .config import PolicyConfig, RateLimitProfile

HARD_DENIED_ACTIONS = frozenset(
    {
        "payment",
        "purchase",
        "buy",
        "transfer_money",
        "financial_transaction",
        "subscribe_paid",
        "credential_access",
        "read_secret",
        "mfa",
        "oauth_authorize",
        "account_security",
        "install_software",
        "elevate",
        "registry_write",
        "security_setting",
        "permanent_delete",
        "shell",
        "run_command",
        "arbitrary_code",
        "batch_send",
        "add_contact",
        "create_group",
        "mention_all",
    }
)

COMMUNICATION_ACTIONS = frozenset({"reply", "proactive_message"})


class DeterministicPolicy:
    """Policy engine that never delegates authorization to a model."""

    def __init__(
        self,
        repository: CoreRepository,
        config: PolicyConfig | None = None,
        *,
        clock: Callable[[], datetime] | None = None,
    ) -> None:
        self.repository = repository
        self.config = config or PolicyConfig()
        self.clock = clock or (lambda: datetime.now(UTC))
        self.timezone = ZoneInfo(self.config.timezone)

    async def evaluate(self, action: ProposedAction) -> PolicyEvaluation:
        kind = action.kind.casefold().strip()

        if self._hard_denied(kind):
            return await self._decision(
                action, PolicyDecision.DENY, "hard_denial", "action is always forbidden"
            )

        if action.bound_channel is not None and action.channel != action.bound_channel:
            return await self._decision(
                action,
                PolicyDecision.DENY,
                "destination_mismatch",
                "channel differs from broker-owned route",
            )
        if action.bound_chat_id is not None and action.chat_id != action.bound_chat_id:
            return await self._decision(
                action,
                PolicyDecision.DENY,
                "destination_mismatch",
                "chat differs from broker-owned route",
            )

        if self._contains_secret(action):
            return await self._decision(
                action,
                PolicyDecision.DENY,
                "secret_boundary",
                "SECRET data cannot enter a model or message",
            )

        if action.side_effect and await self.repository.is_paused(action.channel):
            return await self._decision(
                action, PolicyDecision.DENY, "runtime_paused", "side effects are paused"
            )

        if kind in COMMUNICATION_ACTIONS:
            return await self._evaluate_communication(action, kind)

        if kind == "mcp_tool":
            if not action.arguments.get("enrolled", False):
                return await self._decision(
                    action,
                    PolicyDecision.ENROLL,
                    "mcp_not_enrolled",
                    "tool must be pinned and enrolled by an administrator",
                )
            if action.side_effect:
                return await self._decision(
                    action,
                    PolicyDecision.APPROVE_ONCE,
                    "mcp_side_effect",
                    "enrolled MCP side effect requires one-time approval",
                )
            return await self._decision(
                action, PolicyDecision.AUTO, "mcp_read_enrolled", "enrolled read-only MCP tool"
            )

        if kind in self.config.approval_actions:
            return await self._decision(
                action,
                PolicyDecision.APPROVE_ONCE,
                "explicit_approval",
                "action requires one-time administrator approval",
            )
        if kind in self.config.auto_actions:
            return await self._decision(
                action,
                PolicyDecision.AUTO,
                "safe_capability",
                "action is within an enrolled safe capability",
            )

        return await self._decision(
            action, PolicyDecision.DENY, "unknown_action", "unknown actions are denied by default"
        )

    async def _evaluate_communication(self, action: ProposedAction, kind: str) -> PolicyEvaluation:
        if action.channel is None or action.chat_id is None:
            return await self._decision(
                action,
                PolicyDecision.DENY,
                "missing_destination",
                "communication requires a broker-owned destination",
            )
        if not await self.repository.is_allowlisted(action.channel, action.chat_id):
            return await self._decision(
                action,
                PolicyDecision.ENROLL,
                "chat_not_allowlisted",
                "chat is not enrolled in the allowlist",
            )

        limits = self.config.limits_for(action.channel)
        if action.proactive or kind == "proactive_message":
            return await self._evaluate_proactive(action, limits)
        return await self._evaluate_reply(action, limits)

    async def _evaluate_reply(
        self, action: ProposedAction, limits: RateLimitProfile
    ) -> PolicyEvaluation:
        assert action.channel is not None and action.chat_id is not None
        now = self._now()
        excluded = self._message_id(action)
        windows = (
            (timedelta(minutes=10), limits.reply_per_10_minutes, "reply_10m"),
            (timedelta(hours=1), limits.reply_per_hour, "reply_hour"),
            (timedelta(days=1), limits.reply_per_day, "reply_day"),
        )
        for duration, maximum, rule in windows:
            count = await self.repository.count_outbound_since(
                now - duration,
                channel=action.channel,
                chat_id=action.chat_id,
                exclude_message_id=excluded,
            )
            if count >= maximum:
                return await self._decision(
                    action,
                    PolicyDecision.DENY,
                    f"rate_{rule}",
                    f"reply quota reached ({count}/{maximum})",
                )
        global_count = await self.repository.count_outbound_since(
            now - timedelta(days=1), channel=action.channel, exclude_message_id=excluded
        )
        if global_count >= limits.global_per_day:
            return await self._decision(
                action,
                PolicyDecision.DENY,
                "rate_global_day",
                f"channel daily quota reached ({global_count}/{limits.global_per_day})",
            )
        return await self._decision(
            action, PolicyDecision.AUTO, "reply_allowed", "allowlisted reply within quota"
        )

    async def _evaluate_proactive(
        self, action: ProposedAction, limits: RateLimitProfile
    ) -> PolicyEvaluation:
        assert action.channel is not None and action.chat_id is not None
        excluded = self._message_id(action)
        if not limits.proactive_enabled:
            return await self._decision(
                action,
                PolicyDecision.DENY,
                "proactive_disabled",
                "proactive messaging is disabled for this channel",
            )
        if not action.reason_event_id:
            return await self._decision(
                action,
                PolicyDecision.DENY,
                "proactive_no_reason",
                "proactive messages require a stored reason event",
            )
        now = self._now()
        if self._is_quiet(now.astimezone(self.timezone)):
            return await self._decision(
                action,
                PolicyDecision.DENY,
                "quiet_hours",
                "proactive messaging is blocked during quiet hours",
            )
        cooldown_count = await self.repository.count_outbound_since(
            now - timedelta(hours=limits.proactive_cooldown_hours),
            channel=action.channel,
            chat_id=action.chat_id,
            proactive=True,
            exclude_message_id=excluded,
        )
        if cooldown_count:
            return await self._decision(
                action,
                PolicyDecision.DENY,
                "proactive_cooldown",
                "per-chat proactive cooldown is active",
            )
        chat_day = await self.repository.count_outbound_since(
            now - timedelta(days=1),
            channel=action.channel,
            chat_id=action.chat_id,
            proactive=True,
            exclude_message_id=excluded,
        )
        if chat_day >= limits.proactive_per_day:
            return await self._decision(
                action,
                PolicyDecision.DENY,
                "proactive_chat_day",
                "per-chat proactive daily quota reached",
            )
        global_day = await self.repository.count_outbound_since(
            now - timedelta(days=1),
            channel=action.channel,
            proactive=True,
            exclude_message_id=excluded,
        )
        if global_day >= limits.proactive_global_per_day:
            return await self._decision(
                action,
                PolicyDecision.DENY,
                "proactive_global_day",
                "global proactive daily quota reached",
            )
        return await self._decision(
            action,
            PolicyDecision.AUTO,
            "proactive_allowed",
            "allowlisted proactive message within quota",
        )

    def _now(self) -> datetime:
        value = self.clock()
        if value.tzinfo is None:
            raise ValueError("policy clock must return a timezone-aware datetime")
        return value.astimezone(UTC)

    def _is_quiet(self, local_now: datetime) -> bool:
        current = local_now.timetz().replace(tzinfo=None)
        start = self.config.quiet_start
        end = self.config.quiet_end
        if start == end:
            return True
        if start < end:
            return start <= current < end
        return current >= start or current < end

    @staticmethod
    def _hard_denied(kind: str) -> bool:
        return kind in HARD_DENIED_ACTIONS or any(
            kind.startswith(prefix)
            for prefix in ("payment.", "purchase.", "shell.", "credential.", "delete.permanent")
        )

    @staticmethod
    def _contains_secret(action: ProposedAction) -> bool:
        value = action.arguments.get("data_class")
        if isinstance(value, DataClass):
            value = value.value
        is_secret = isinstance(value, str) and value.casefold() == DataClass.SECRET.value.casefold()
        return is_secret and action.kind.casefold() in {
            "reply",
            "proactive_message",
            "model_call",
            "mcp_tool",
            "mcp_read",
            "mcp_write",
        }

    @staticmethod
    def _message_id(action: ProposedAction) -> UUID | None:
        value = action.arguments.get("message_id")
        if value is None:
            return None
        try:
            return UUID(str(value))
        except (TypeError, ValueError, AttributeError):
            return None

    async def _decision(
        self,
        action: ProposedAction,
        decision: PolicyDecision,
        rule_id: str,
        reason: str,
    ) -> PolicyEvaluation:
        evaluation = PolicyEvaluation(decision=decision, rule_id=rule_id, reason=reason)
        await self.repository.append_audit(
            AuditRecord(
                action=f"policy.{action.kind}",
                outcome=decision.value,
                channel=action.channel,
                chat_id=action.chat_id,
                event_id=action.reason_event_id,
                rule_id=rule_id,
                detail={"proactive": action.proactive, "side_effect": action.side_effect},
            )
        )
        return evaluation

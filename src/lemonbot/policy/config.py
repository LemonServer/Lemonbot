"""Policy defaults.  Values are deliberately conservative and overridable."""

from __future__ import annotations

from datetime import time

from pydantic import BaseModel, ConfigDict, Field


class RateLimitProfile(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    reply_per_10_minutes: int = Field(ge=1)
    reply_per_hour: int = Field(ge=1)
    reply_per_day: int = Field(ge=1)
    global_per_day: int = Field(ge=1)
    proactive_cooldown_hours: int = Field(ge=1)
    proactive_per_day: int = Field(ge=1)
    proactive_global_per_day: int = Field(ge=1)
    proactive_enabled: bool = False


WECHAT_LAB_LIMITS = RateLimitProfile(
    reply_per_10_minutes=3,
    reply_per_hour=10,
    reply_per_day=30,
    global_per_day=50,
    proactive_cooldown_hours=12,
    proactive_per_day=2,
    proactive_global_per_day=10,
    proactive_enabled=False,
)


class PolicyConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    timezone: str = "Asia/Shanghai"
    quiet_start: time = time(23, 0)
    quiet_end: time = time(8, 0)
    wechat_lab: RateLimitProfile = WECHAT_LAB_LIMITS
    fallback: RateLimitProfile = WECHAT_LAB_LIMITS
    auto_actions: frozenset[str] = frozenset(
        {
            "reply",
            "model_call",
            "summarize",
            "memory_read",
            "memory_write",
            "browse_public_https",
            "vision_read",
            "read_file",
            "mcp_read",
        }
    )
    approval_actions: frozenset[str] = frozenset({"write_file", "mcp_write"})

    def limits_for(self, channel: str) -> RateLimitProfile:
        normalised = channel.casefold()
        if normalised in {
            "wechat",
            "wechat_lab",
            "personal_wechat",
            "wechat_personal_lab",
        }:
            return self.wechat_lab
        return self.fallback

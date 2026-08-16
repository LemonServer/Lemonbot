"""Provider configuration and deterministic model routing."""

from __future__ import annotations

from enum import StrEnum
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, field_validator

from lemonbot.domain.models import ModelRequest

DEEPSEEK_BASE_URL = "https://api.deepseek.com"
DEEPSEEK_FLASH_MODEL = "deepseek-v4-flash"
DEEPSEEK_PRO_MODEL = "deepseek-v4-pro"
ZHIPU_BASE_URL = "https://open.bigmodel.cn/api/paas/v4"
ZHIPU_VISION_MODEL = "glm-4.6v-flash"


class ModelTier(StrEnum):
    FLASH = "flash"
    PRO = "pro"


class ProviderConfig(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str = Field(min_length=1, max_length=64)
    base_url: str
    # ``None`` is supported only for explicitly configured unauthenticated
    # local OpenAI-compatible servers (for example Ollama on loopback).
    secret_name: str | None = Field(default=None, min_length=1, max_length=128)
    flash_model: str = Field(min_length=1, max_length=256)
    pro_model: str = Field(min_length=1, max_length=256)
    timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    context_tokens: int = Field(default=131_072, ge=1)
    enable_thinking_on_pro: bool = True

    @field_validator("base_url")
    @classmethod
    def validate_base_url(cls, value: str) -> str:
        parsed = urlsplit(value)
        if parsed.scheme not in {"http", "https"} or not parsed.hostname:
            raise ValueError("provider base_url must be an absolute HTTP(S) URL")
        if parsed.username or parsed.password or parsed.query or parsed.fragment:
            raise ValueError("provider base_url cannot contain credentials, query, or fragment")
        return value.rstrip("/")

    @classmethod
    def deepseek(cls) -> ProviderConfig:
        return cls(
            provider="deepseek",
            base_url=DEEPSEEK_BASE_URL,
            secret_name="deepseek_api_key",  # noqa: S106 - credential lookup identifier
            flash_model=DEEPSEEK_FLASH_MODEL,
            pro_model=DEEPSEEK_PRO_MODEL,
        )


class RoutedModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)

    provider: str
    model: str
    tier: ModelTier
    thinking: bool = False


class DeterministicRouter:
    """Route only from broker-owned request flags, never model output."""

    def __init__(self, config: ProviderConfig) -> None:
        self._config = config

    def route(self, request: ModelRequest) -> RoutedModel:
        if request.deep:
            return RoutedModel(
                provider=self._config.provider,
                model=self._config.pro_model,
                tier=ModelTier.PRO,
                thinking=self._config.enable_thinking_on_pro,
            )
        return RoutedModel(
            provider=self._config.provider,
            model=self._config.flash_model,
            tier=ModelTier.FLASH,
            thinking=False,
        )

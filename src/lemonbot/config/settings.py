from __future__ import annotations

import os
import re
import tomllib
from datetime import time
from decimal import Decimal
from pathlib import Path, PureWindowsPath
from typing import Literal

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from lemonbot.tools.mcp import PinnedMCPServer


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeSettings(StrictModel):
    connector: Literal["fake", "wecom", "wechat_uia"] = "fake"
    data_root: str = ""
    log_level: Literal["DEBUG", "INFO", "WARNING", "ERROR", "CRITICAL"] = "INFO"


class AdminSettings(StrictModel):
    host: Literal["127.0.0.1", "::1"] = "127.0.0.1"
    port: int = Field(default=8765, ge=1024, le=65535)


class BudgetSettings(StrictModel):
    enabled: bool = False
    daily_limit_cny: Decimal = Field(default=Decimal(0), ge=0)
    monthly_limit_cny: Decimal = Field(default=Decimal(0), ge=0)
    flash_input_cny_per_million: Decimal = Field(default=Decimal(0), ge=0)
    flash_output_cny_per_million: Decimal = Field(default=Decimal(0), ge=0)
    pro_input_cny_per_million: Decimal = Field(default=Decimal(0), ge=0)
    pro_output_cny_per_million: Decimal = Field(default=Decimal(0), ge=0)

    @model_validator(mode="after")
    def validate_enabled_budget(self) -> BudgetSettings:
        values = (
            self.daily_limit_cny,
            self.monthly_limit_cny,
            self.flash_input_cny_per_million,
            self.flash_output_cny_per_million,
            self.pro_input_cny_per_million,
            self.pro_output_cny_per_million,
        )
        if self.enabled and any(value <= 0 for value in values):
            raise ValueError("enabled cloud models require positive limits and explicit prices")
        return self


class ModelSettings(StrictModel):
    provider: Literal["fake", "deepseek", "openai_compatible"] = "deepseek"
    base_url: HttpUrl = HttpUrl("https://api.deepseek.com")
    api_key_secret_name: str = Field(default="deepseek_api_key", max_length=128)
    flash_model: str = "deepseek-v4-flash"
    pro_model: str = "deepseek-v4-pro"
    request_timeout_seconds: float = Field(default=90, gt=0, le=300)
    max_input_tokens: int = Field(default=65_536, ge=1024, le=1_000_000)
    max_output_tokens: int = Field(default=4096, ge=128, le=65_536)
    verify_models_on_startup: bool = True
    budget: BudgetSettings = Field(default_factory=BudgetSettings)


class VisionSettings(StrictModel):
    enabled: bool = False
    base_url: HttpUrl = HttpUrl("https://open.bigmodel.cn/api/paas/v4")
    model: str = "glm-4.6v-flash"
    max_file_bytes: int = Field(default=10 * 1024 * 1024, ge=1024, le=50 * 1024 * 1024)
    max_pixels: int = Field(default=20_000_000, ge=10_000, le=50_000_000)
    image_token_reserve: int = Field(default=16_384, ge=8_192, le=128_000)
    input_cny_per_million: Decimal | None = Field(default=None, ge=0)
    output_cny_per_million: Decimal | None = Field(default=None, ge=0)

    @model_validator(mode="after")
    def require_explicit_price(self) -> VisionSettings:
        if self.enabled and (
            self.input_cny_per_million is None or self.output_cny_per_million is None
        ):
            raise ValueError(
                "enabled vision requires an explicit price, including an explicit zero"
            )
        return self


class BrowserSettings(StrictModel):
    enabled: bool = False
    max_text_chars: int = Field(default=50_000, ge=1000, le=500_000)
    navigation_timeout_seconds: float = Field(default=30, gt=0, le=120)
    max_downloads_per_task: int = Field(default=3, ge=0, le=10)


class VaultSettings(StrictModel):
    read_roots: tuple[str, ...] = ()
    write_roots: tuple[str, ...] = ()


class MCPServerSettings(PinnedMCPServer):
    """A fully pinned MCP process command and exact reported server version."""


class MCPSettings(StrictModel):
    enabled: bool = False
    servers: tuple[MCPServerSettings, ...] = ()

    @model_validator(mode="after")
    def validate_server_names(self) -> MCPSettings:
        names = [server.name for server in self.servers]
        if len(names) != len(set(names)):
            raise ValueError("MCP server names must be unique")
        if self.enabled and not any(server.enabled for server in self.servers):
            raise ValueError("enabled MCP requires at least one explicitly enabled server")
        return self


class WeComSettings(StrictModel):
    enabled: bool = False
    bot_id: str = ""
    welcome_text: str = "你好，我是 Lemonbot，一个由 AI 驱动的助手。"
    allow_chat_ids: tuple[str, ...] = ()
    admin_sender_ids: tuple[str, ...] = ()

    @model_validator(mode="after")
    def require_identity_and_allowlist_when_enabled(self) -> WeComSettings:
        if self.enabled and (not self.bot_id.strip() or not self.allow_chat_ids):
            raise ValueError("enabled WeCom requires bot_id and a stable chat allowlist")
        return self


class WechatUIASettings(StrictModel):
    enabled: bool = False
    stage: Literal["observe", "draft", "reply", "proactive"] = "observe"
    expected_account: str = ""
    expected_windows_user: str = ""
    expected_process_name: str = "WeChat.exe"
    expected_executable_path: str = ""
    expected_executable_sha256: str = ""
    enrolled_client_version: str = ""
    enrolled_selector_signature: str = ""
    selector_bundle_path: str = ""
    allow_chat_ids: tuple[str, ...] = ()
    admin_sender_ids: tuple[str, ...] = ()
    reconcile_seconds: float = Field(default=15, ge=5, le=300)

    @model_validator(mode="after")
    def require_enrollment_when_enabled(self) -> WechatUIASettings:
        if self.expected_executable_path:
            path = PureWindowsPath(self.expected_executable_path)
            if (
                not path.is_absolute()
                or len(path.drive) != 2
                or not path.drive.endswith(":")
                or any(part in {".", ".."} for part in path.parts)
                or self.expected_executable_path.startswith(
                    ("\\\\", "\\\\?\\", "\\\\.\\")
                )
            ):
                raise ValueError(
                    "expected_executable_path must be an absolute local-drive path"
                )
            if path.name.casefold() != self.expected_process_name.casefold():
                raise ValueError(
                    "expected executable filename must match expected_process_name"
                )
        if self.expected_executable_sha256 and re.fullmatch(
            r"[0-9a-f]{64}", self.expected_executable_sha256
        ) is None:
            raise ValueError(
                "expected_executable_sha256 must be 64 lowercase hex characters"
            )
        enrollment = (
            self.expected_account,
            self.expected_windows_user,
            self.expected_executable_path,
            self.expected_executable_sha256,
            self.enrolled_client_version,
            self.enrolled_selector_signature,
            self.selector_bundle_path,
        )
        if self.enabled and (any(not value for value in enrollment) or not self.allow_chat_ids):
            raise ValueError(
                "enabled personal WeChat requires account/user/executable/version/selectors "
                "and an allowlist"
            )
        return self


class ReplyLimit(StrictModel):
    per_10_minutes: int = Field(ge=1)
    per_hour: int = Field(ge=1)
    per_day: int = Field(ge=1)
    global_per_day: int = Field(ge=1)


class ProactiveLimit(StrictModel):
    per_period: int = Field(ge=1)
    period_hours: int = Field(ge=1, le=24)
    per_day: int = Field(ge=1)
    global_per_day: int = Field(ge=1)


class LimitsSettings(StrictModel):
    quiet_start: time = time(23, 0)
    quiet_end: time = time(8, 0)
    event_timeout_seconds: int = Field(default=300, ge=10, le=1800)
    max_model_turns: int = Field(default=8, ge=1, le=32)
    max_tool_calls: int = Field(default=20, ge=0, le=100)
    max_navigations: int = Field(default=10, ge=0, le=50)
    max_downloads: int = Field(default=3, ge=0, le=10)
    max_reply_chunks: int = Field(default=2, ge=1, le=2)
    max_chunk_chars: int = Field(default=1500, ge=100, le=1500)
    wecom_reply: ReplyLimit = Field(
        default_factory=lambda: ReplyLimit(
            per_10_minutes=6, per_hour=30, per_day=100, global_per_day=500
        )
    )
    wecom_proactive: ProactiveLimit = Field(
        default_factory=lambda: ProactiveLimit(
            per_period=1, period_hours=6, per_day=3, global_per_day=30
        )
    )
    wechat_reply: ReplyLimit = Field(
        default_factory=lambda: ReplyLimit(
            per_10_minutes=3, per_hour=10, per_day=30, global_per_day=50
        )
    )
    wechat_proactive: ProactiveLimit = Field(
        default_factory=lambda: ProactiveLimit(
            per_period=1, period_hours=12, per_day=2, global_per_day=10
        )
    )


class AppSettings(StrictModel):
    schema_version: Literal[1] = 1
    profile: Literal["prod", "lab"] = "prod"
    timezone: str = "Asia/Shanghai"
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    admin: AdminSettings = Field(default_factory=AdminSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    vision: VisionSettings = Field(default_factory=VisionSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    vault: VaultSettings = Field(default_factory=VaultSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    wecom: WeComSettings = Field(default_factory=WeComSettings)
    wechat_uia: WechatUIASettings = Field(default_factory=WechatUIASettings)
    limits: LimitsSettings = Field(default_factory=LimitsSettings)

    @model_validator(mode="after")
    def validate_channel_isolation(self) -> AppSettings:
        if self.runtime.connector == "wecom" and self.profile != "prod":
            raise ValueError("the WeCom connector must use the prod profile")
        if self.runtime.connector == "wechat_uia" and self.profile != "lab":
            raise ValueError("the personal WeChat connector must use the lab profile")
        if self.runtime.connector == "wechat_uia" and not self.wechat_uia.enabled:
            raise ValueError("wechat_uia connector requires explicit enabled=true")
        if self.runtime.connector == "wecom" and not self.wecom.enabled:
            raise ValueError("wecom connector requires explicit enabled=true")
        if (
            self.models.provider == "deepseek"
            and str(self.models.base_url).rstrip("/") != "https://api.deepseek.com"
        ):
            raise ValueError(
                "DeepSeek provider must use the official https://api.deepseek.com endpoint"
            )
        if self.models.provider == "deepseek" and not self.models.api_key_secret_name:
            raise ValueError("DeepSeek requires a Windows Credential Manager secret")
        if self.models.provider == "openai_compatible":
            model_url = self.models.base_url
            host = (model_url.host or "").casefold()
            if model_url.scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("plaintext OpenAI-compatible endpoints must be loopback-only")
        if self.vision.enabled and (
            str(self.vision.base_url).rstrip("/")
            != "https://open.bigmodel.cn/api/paas/v4"
            or self.vision.model != "glm-4.6v-flash"
        ):
            raise ValueError(
                "enabled vision must use the official Zhipu endpoint and glm-4.6v-flash"
            )
        if self.vision.enabled and self.models.provider == "fake":
            raise ValueError("cloud vision cannot share a disabled fake-model budget")
        return self


def default_config_path() -> Path:
    override = os.environ.get("LEMONBOT_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    local = os.environ.get("LOCALAPPDATA")
    if local:
        return Path(local) / "Lemonbot" / "config.toml"
    from platformdirs import user_config_path

    return user_config_path("Lemonbot") / "config.toml"


def load_settings(path: Path | None = None) -> AppSettings:
    config_path = (path or default_config_path()).expanduser().resolve()
    with config_path.open("rb") as stream:
        raw = tomllib.load(stream)
    return AppSettings.model_validate(raw)

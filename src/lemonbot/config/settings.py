from __future__ import annotations

import os
import re
import tomllib
from datetime import time
from decimal import Decimal
from pathlib import Path, PurePosixPath
from typing import Literal
from urllib.parse import urlsplit

from pydantic import BaseModel, ConfigDict, Field, HttpUrl, model_validator

from lemonbot.safety_gates import AT_SPI_DIRECTION_GATE_OPEN
from lemonbot.tools.mcp import PinnedMCPServer


class StrictModel(BaseModel):
    model_config = ConfigDict(extra="forbid", frozen=True)


class RuntimeSettings(StrictModel):
    connector: Literal["fake", "wechat_atspi", "wechat_agent"] = "fake"
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
    provider: Literal["disabled", "fake", "deepseek", "openai_compatible"] = "disabled"
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
    # Keep the complete ToolResult inside the 1 MiB framed-JSON IPC ceiling.
    max_text_chars: int = Field(default=50_000, ge=1000, le=200_000)
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


class WechatAgentSettings(StrictModel):
    enabled: bool = False
    mode: Literal["canary", "conversation"] = "canary"
    token_file: str = ""
    state_dir: str = ""
    whitelist_file: str = ""
    memory_enabled: bool = False

    @model_validator(mode="after")
    def validate_paths(self) -> WechatAgentSettings:
        if self.enabled and not all((self.token_file, self.state_dir)):
            raise ValueError("enabled wechat_agent requires private token and binding paths")
        for value in (self.token_file, self.state_dir, self.whitelist_file):
            if value and not Path(value).expanduser().is_absolute():
                raise ValueError("wechat_agent paths must be absolute")
        if self.whitelist_file and self.mode != "conversation":
            raise ValueError("wechat whitelist requires conversation mode")
        if self.memory_enabled and self.mode != "conversation":
            raise ValueError("wechat memory requires conversation mode")
        return self


class ConversationCapabilities(StrictModel):
    """Opt-in, read-only capabilities for the bound conversation runtime."""

    images_enabled: bool = False
    web_read_enabled: bool = False
    search_enabled: bool = False
    clock_enabled: bool = False
    image_retention_days: int = Field(default=7, ge=1, le=30)
    search_url: str = "http://127.0.0.1:6177/search"
    search_image: str = (
        "ghcr.io/searxng/searxng@sha256:"
        "ef6ae36e01f89f8dadcabc684a9159728fbda6f0be8d638edce26d0a3ede17c6"
    )
    search_daily_limit: int = Field(default=20, ge=1, le=20)
    search_monthly_limit: int = Field(default=600, ge=1, le=600)

    @model_validator(mode="after")
    def constrain_search(self) -> ConversationCapabilities:
        parsed = urlsplit(self.search_url)
        if (
            parsed.scheme != "http"
            or parsed.hostname != "127.0.0.1"
            or parsed.path != "/search"
            or parsed.query
            or parsed.fragment
            or parsed.username
            or parsed.password
        ):
            raise ValueError("search must use the private loopback SearXNG endpoint")
        if not re.fullmatch(
            r"(?:ghcr\.io/searxng/searxng@)?sha256:[a-f0-9]{64}", self.search_image
        ):
            raise ValueError("search image must be a pinned official digest or local image ID")
        return self

    @property
    def enabled(self) -> bool:
        return any(
            (self.images_enabled, self.web_read_enabled, self.search_enabled, self.clock_enabled)
        )


class WechatAtspiSettings(StrictModel):
    enabled: bool = False
    stage: Literal["observe"] = "observe"
    expected_linux_uid: int = Field(default=1000, ge=1)
    expected_linux_user: str = ""
    expected_session_type: Literal["wayland"] = "wayland"
    expected_executable_path: str = "/opt/wechat/wechat"
    worker_python_path: str = ""
    expected_executable_sha256: str = ""
    enrolled_client_version: str = ""
    account_fingerprint: str = ""
    ui_signature: str = ""
    enrollment_bundle_path: str = ""
    enrollment_bundle_sha256: str = ""
    allow_target_refs: tuple[str, ...] = ()
    event_debounce_ms: int = Field(default=500, ge=100, le=5_000)
    reconcile_seconds: float = Field(default=15, ge=5, le=300)

    @model_validator(mode="after")
    def require_enrollment_when_enabled(self) -> WechatAtspiSettings:
        if (
            self.expected_linux_user
            and re.fullmatch(r"[a-z_][a-z0-9_-]{0,31}", self.expected_linux_user) is None
        ):
            raise ValueError("expected_linux_user is not a safe Linux user name")
        if self.expected_executable_path:
            path = PurePosixPath(self.expected_executable_path)
            if not path.is_absolute() or any(part in {".", ".."} for part in path.parts):
                raise ValueError("expected_executable_path must be an absolute POSIX path")
        enrollment = (
            self.expected_linux_user,
            self.expected_executable_path,
            self.worker_python_path,
            self.expected_executable_sha256,
            self.enrolled_client_version,
            self.account_fingerprint,
            self.ui_signature,
            self.enrollment_bundle_path,
            self.enrollment_bundle_sha256,
        )
        if self.worker_python_path:
            worker_path = PurePosixPath(self.worker_python_path)
            if not worker_path.is_absolute() or any(
                part in {".", ".."} for part in worker_path.parts
            ):
                raise ValueError("worker_python_path must be an absolute POSIX path")
        digests = (
            self.expected_executable_sha256,
            self.account_fingerprint,
            self.ui_signature,
            self.enrollment_bundle_sha256,
        )
        if any(value and len(value) != 64 for value in digests) or any(
            value and any(character not in "0123456789abcdef" for character in value)
            for value in digests
        ):
            raise ValueError("AT-SPI fingerprints must be 64 lowercase hex characters")
        if len(set(self.allow_target_refs)) != len(self.allow_target_refs):
            raise ValueError("allow_target_refs must not contain duplicates")
        if any(
            not value
            or len(value) > 128
            or any(ch not in "abcdefghijklmnopqrstuvwxyz0123456789_-" for ch in value)
            for value in self.allow_target_refs
        ):
            raise ValueError("target refs must use lowercase safe identifiers")
        if self.enabled and (any(not value for value in enrollment) or not self.allow_target_refs):
            raise ValueError(
                "enabled AT-SPI requires Linux identity, executable, enrollment fingerprints "
                "and an allowlist"
            )
        return self


class ReplyLimit(StrictModel):
    per_10_minutes: int = Field(ge=0, description="0 disables this window")
    per_hour: int = Field(ge=0, description="0 disables this window")
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
    delivery_timeout_seconds: int = Field(default=60, ge=5, le=300)
    max_model_turns: int = Field(default=8, ge=1, le=32)
    max_task_input_tokens: int = Field(default=196_608, ge=1_024, le=2_000_000)
    max_task_output_tokens: int = Field(default=16_384, ge=128, le=131_072)
    max_tool_calls: int = Field(default=20, ge=0, le=100)
    max_navigations: int = Field(default=10, ge=0, le=50)
    max_downloads: int = Field(default=3, ge=0, le=10)
    max_reply_chunks: int = Field(default=2, ge=1, le=2)
    max_chunk_chars: int = Field(default=1500, ge=100, le=1500)
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


class ContextMemorySettings(StrictModel):
    """Staged unified-context configuration; no live implementation in stage 1."""

    enabled: bool = False
    archive_enabled: bool = False
    consolidation_enabled: bool = False
    consolidation_min_messages: int = Field(default=20, ge=20, le=100)
    consolidation_daily_cny: Decimal = Field(default=Decimal("0.50"), gt=0, le=Decimal("0.50"))
    embedding_backend: Literal["tensorrt", "cpu"] = "tensorrt"
    embedding_precision: Literal["fp16", "fp32"] = "fp16"
    embedding_worker_python: str = Field(default="", max_length=4096)
    embedding_assets_root: str = Field(default="", max_length=4096)
    embedding_timeout_seconds: float = Field(default=30, gt=0, le=300)
    embedding_cache_entries: int = Field(default=4096, ge=1, le=100_000)
    retrieval_max_messages: int = Field(default=10000, ge=1, le=10000)
    retrieval_vector_chunks_per_refresh: int = Field(default=256, ge=1, le=4096)
    retrieval_semantic_min_similarity: float = Field(default=0.5, ge=0, le=1)
    retrieval_candidate_char_budget: int = Field(default=16000, ge=2048, le=32000)
    retrieval_recent_messages: int = Field(default=6, ge=0, le=20)

    @model_validator(mode="after")
    def prevent_premature_activation(self) -> ContextMemorySettings:
        if self.consolidation_enabled and not self.enabled:
            raise ValueError("unified consolidation requires unified context enabled")
        for value in (self.embedding_worker_python, self.embedding_assets_root):
            if value and not Path(value).expanduser().is_absolute():
                raise ValueError("embedding paths must be absolute")
        return self


class AppSettings(StrictModel):
    schema_version: Literal[2] = 2
    profile: Literal["prod", "lab"] = "prod"
    timezone: str = "Asia/Shanghai"
    runtime: RuntimeSettings = Field(default_factory=RuntimeSettings)
    admin: AdminSettings = Field(default_factory=AdminSettings)
    models: ModelSettings = Field(default_factory=ModelSettings)
    vision: VisionSettings = Field(default_factory=VisionSettings)
    browser: BrowserSettings = Field(default_factory=BrowserSettings)
    vault: VaultSettings = Field(default_factory=VaultSettings)
    mcp: MCPSettings = Field(default_factory=MCPSettings)
    wechat_agent: WechatAgentSettings = Field(default_factory=WechatAgentSettings)
    conversation: ConversationCapabilities = Field(default_factory=ConversationCapabilities)
    context_memory: ContextMemorySettings = Field(default_factory=ContextMemorySettings)
    wechat_atspi: WechatAtspiSettings = Field(default_factory=WechatAtspiSettings)
    limits: LimitsSettings = Field(default_factory=LimitsSettings)

    @model_validator(mode="after")
    def validate_channel_isolation(self) -> AppSettings:
        if self.context_memory.enabled and (
            not self.context_memory.archive_enabled
            or self.runtime.connector != "wechat_agent"
            or self.wechat_agent.mode != "conversation"
            or self.wechat_agent.memory_enabled
            or self.limits.max_model_turns < 2
        ):
            raise ValueError(
                "unified context requires archive, bound conversation runtime, "
                "at least two model turns, and legacy memory disabled"
            )
        if self.context_memory.archive_enabled and (
            self.runtime.connector != "wechat_agent" or self.wechat_agent.mode != "conversation"
        ):
            raise ValueError("unified archive requires the bound conversation runtime")
        if self.conversation.enabled and (
            self.runtime.connector != "wechat_agent" or self.wechat_agent.mode != "conversation"
        ):
            raise ValueError("conversation capabilities require the bound conversation runtime")
        if self.runtime.connector == "wechat_agent":
            if self.profile != "lab" or not self.wechat_agent.enabled:
                raise ValueError("wechat_agent requires lab profile and explicit enabled=true")
            if any(
                (
                    self.vision.enabled,
                    self.browser.enabled,
                    self.mcp.enabled,
                    self.vault.read_roots,
                    self.vault.write_roots,
                )
            ):
                raise ValueError("wechat_agent requires models and tools disabled")
            if self.wechat_agent.mode == "canary" and self.models.provider != "disabled":
                raise ValueError("wechat_agent canary requires models disabled")
            if self.wechat_agent.mode == "conversation" and (
                self.models.provider != "deepseek"
                or self.models.flash_model != "deepseek-flash"
                or self.models.pro_model != "deepseek-flash"
                or not self.models.budget.enabled
                or self.models.max_input_tokens > 8192
                or self.models.max_output_tokens > 512
            ):
                raise ValueError("wechat_agent conversation requires budgeted bounded Flash text")
            if not self.runtime.data_root:
                raise ValueError("wechat_agent requires an independent runtime data_root")
        if self.runtime.connector == "wechat_atspi" and self.profile != "lab":
            raise ValueError("the personal WeChat AT-SPI connector must use the lab profile")
        if self.runtime.connector == "wechat_atspi" and not self.wechat_atspi.enabled:
            raise ValueError("wechat_atspi connector requires explicit enabled=true")
        if self.runtime.connector == "wechat_atspi" and self.models.provider != "disabled":
            raise ValueError("observe-only AT-SPI requires models.provider='disabled'")
        if self.runtime.connector == "wechat_atspi" and not AT_SPI_DIRECTION_GATE_OPEN:
            raise ValueError("wechat_atspi is disabled because message direction is unproven")
        if (
            self.models.provider == "deepseek"
            and str(self.models.base_url).rstrip("/") != "https://api.deepseek.com"
        ):
            raise ValueError(
                "DeepSeek provider must use the official https://api.deepseek.com endpoint"
            )
        if self.models.provider == "deepseek" and not self.models.api_key_secret_name:
            raise ValueError("DeepSeek requires a secure credential-store secret")
        if self.models.provider == "openai_compatible":
            model_url = self.models.base_url
            host = (model_url.host or "").casefold()
            if model_url.scheme == "http" and host not in {"127.0.0.1", "localhost", "::1"}:
                raise ValueError("plaintext OpenAI-compatible endpoints must be loopback-only")
        if self.vision.enabled and (
            str(self.vision.base_url).rstrip("/") != "https://open.bigmodel.cn/api/paas/v4"
            or self.vision.model != "glm-4.6v-flash"
        ):
            raise ValueError(
                "enabled vision must use the official Zhipu endpoint and glm-4.6v-flash"
            )
        if self.vision.enabled and self.models.provider == "fake":
            raise ValueError("cloud vision cannot share a disabled fake-model budget")
        return self


def checkout_root() -> Path | None:
    """Locate this source checkout, independently of the caller's working directory."""
    root = Path(__file__).resolve().parents[3]
    if (root / "pyproject.toml").is_file() and (root / "src/lemonbot/config/settings.py").is_file():
        return root
    return None


def default_config_path() -> Path:
    override = os.environ.get("LEMONBOT_CONFIG")
    if override:
        return Path(override).expanduser().resolve()
    root = checkout_root()
    if root is not None:
        return root / "config/lemonbot.toml"
    from platformdirs import user_config_path

    return user_config_path("Lemonbot") / "config.toml"


def resolve_config_path(path: Path | None = None) -> Path:
    return (path if path is not None else default_config_path()).expanduser().resolve()


def load_settings(path: Path | None = None) -> AppSettings:
    config_path = resolve_config_path(path)
    with config_path.open("rb") as stream:
        raw = tomllib.load(stream)
    return AppSettings.model_validate(raw)

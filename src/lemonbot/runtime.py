from __future__ import annotations

import asyncio
import logging
import sys
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import uvicorn
from jsonschema import validate  # type: ignore[import-untyped]

from lemonbot.admin import create_admin_app
from lemonbot.admin.auth import LocalTokenManager
from lemonbot.admin.control import ApprovalView, ControlBackend, StatusView
from lemonbot.admin.tray import start_tray
from lemonbot.approvals import ApprovalClaim, ApprovalRepository, ApprovalService
from lemonbot.config import AppSettings, RuntimePaths
from lemonbot.connectors import (
    FakeConnector,
    PersonalWeChatConfig,
    PersonalWeChatConnector,
    PersonalWeChatStage,
    SelectorBundle,
    WeComConfig,
    WeComConnector,
    WindowsWeChatUIABackend,
)
from lemonbot.domain import (
    ApprovalState,
    Connector,
    InboundEvent,
    ModelBackend,
    PolicyDecision,
    ProposedAction,
    ToolContext,
)
from lemonbot.memory import (
    ContextBuilder,
    MemoryContextService,
    MemoryDerivationService,
    SQLiteMemoryStore,
)
from lemonbot.models import (
    BudgetLimits,
    IsolatedModelBackend,
    ModelPrice,
    ModelWorkerConfig,
    PersistentBudgetManager,
    ProviderConfig,
    VisionProviderConfig,
    VisionService,
    ZhipuVisionAdapter,
)
from lemonbot.orchestration import EventPipeline, FakeModelBackend, PipelineConfig, PipelineStatus
from lemonbot.policy import DeterministicPolicy, PolicyConfig, RateLimitProfile
from lemonbot.proactive import ProactiveJobStore, ProactiveRunner
from lemonbot.runtime_lock import RuntimeLock
from lemonbot.security.model_secrets import AsyncSecretStoreAdapter
from lemonbot.security.redaction import configure_logging
from lemonbot.security.secrets import NamespacedSecretStore, WindowsCredentialStore
from lemonbot.storage import CoreRepository, Database
from lemonbot.storage.migrate import upgrade_database
from lemonbot.supervisor import WorkerSupervisor
from lemonbot.tools import Tool
from lemonbot.tools.attachments import AttachmentStore
from lemonbot.tools.browser import BrowserReadTool
from lemonbot.tools.mcp import MCPStdioClient, MCPToolAdapter, PinnedMCPServer
from lemonbot.tools.vault import FileVault, VaultCreateTool, VaultReadTool, VaultRoot
from lemonbot.tools.vision import ImagePreprocessor, RapidOCRReader
from lemonbot.tools.vision_tool import ImageUnderstandingTool

logger = logging.getLogger(__name__)


def _pipeline_output_mode(settings: AppSettings) -> Literal["observe", "draft", "send"]:
    if settings.runtime.connector != "wechat_uia":
        return "send"
    if settings.wechat_uia.stage == "observe":
        return "observe"
    if settings.wechat_uia.stage == "draft":
        return "draft"
    return "send"


class RepositoryControl(ControlBackend):
    def __init__(
        self,
        repository: CoreRepository,
        *,
        profile: str,
        connector_name: str,
        started_at: datetime,
        emergency_event: asyncio.Event,
        approvals: ApprovalService,
        tools: Mapping[str, Tool],
        policy: DeterministicPolicy,
        granted_tool_scopes: frozenset[str],
        side_effect_lock: asyncio.Lock,
    ) -> None:
        self._repository = repository
        self._profile = profile
        self._connector_name = connector_name
        self._started_at = started_at
        self._emergency_event = emergency_event
        self._approvals = approvals
        self._tools = dict(tools)
        self._policy = policy
        self._granted_tool_scopes = granted_tool_scopes
        self._side_effect_lock = side_effect_lock

    async def status(self) -> StatusView:
        counts = await self._repository.runtime_counts()
        pending_approvals = await self._approvals.pending()
        return StatusView(
            profile=self._profile,
            connector=self._connector_name,
            global_paused=await self._repository.is_paused(),
            channel_pauses={
                "wecom": await self._repository.is_paused("wecom"),
                "wechat_uia": await self._repository.is_paused("wechat_personal_lab"),
            },
            emergency_stopped=self._emergency_event.is_set(),
            queue_depth=counts["queue_depth"],
            unknown_outbox=counts["unknown_outbox"],
            pending_approvals=len(pending_approvals),
            started_at=self._started_at,
        )

    async def set_pause(self, channel: str | None, paused: bool) -> StatusView:
        if self._emergency_event.is_set() and not paused:
            raise RuntimeError("restart is required after emergency stop")
        mapped = (
            None
            if channel is None
            else {"wecom": "wecom", "wechat_uia": "wechat_personal_lab"}.get(
                channel, channel
            )
        )
        await self._repository.set_paused(channel=mapped, paused=paused)
        return await self.status()

    async def emergency_stop(self) -> StatusView:
        self._emergency_event.set()
        await self._repository.set_paused(paused=True)
        return await self.status()

    async def approvals(self) -> list[ApprovalView]:
        return [
            ApprovalView(
                approval_id=str(item.approval_id),
                created_at=item.created_at,
                expires_at=item.expires_at,
                action_type=item.action_kind,
                summary=f"{item.tool_name}: {item.arguments_summary}",
                channel=item.channel,
                chat_id=item.chat_id,
            )
            for item in await self._approvals.pending()
        ]

    async def decide_approval(
        self, approval_id: str, decision: Literal["approve_once", "deny"]
    ) -> bool:
        if decision == "deny":
            try:
                return await self._approvals.deny(approval_id)
            except ValueError:
                return False
        try:
            claim = await self._approvals.approve_once(approval_id)
        except ValueError:
            return False
        if claim is None:
            return False
        await self._execute_approved_tool(claim)
        return True

    async def _execute_approved_tool(self, claim: ApprovalClaim) -> None:
        """Execute the exact claimed action once; never retry an ambiguous call."""

        outcome = ApprovalState.DENIED
        outcome_code = "approval_revalidation_failed"
        invocation_started = False
        tool_execution_id: str | None = None
        tool_execution_resolved = False
        try:
            async with self._side_effect_lock:
                event = await self._repository.inbound_event(
                    claim.channel,
                    claim.chat_id,
                    claim.event_id,
                )
                tool = self._tools.get(claim.tool_name)
                if event is None or tool is None:
                    return
                manifest = tool.manifest()
                if (
                    not manifest.side_effect
                    or manifest.name != claim.tool_name
                    or manifest.action_kind != claim.action_kind
                    or not await self._repository.is_allowlisted(
                        claim.channel, claim.chat_id
                    )
                ):
                    return
                try:
                    validate(claim.arguments, manifest.input_schema)
                except Exception:
                    outcome_code = "approval_schema_changed"
                    return
                action = ProposedAction(
                    kind=claim.action_kind,
                    channel=claim.channel,
                    chat_id=claim.chat_id,
                    bound_channel=claim.channel,
                    bound_chat_id=claim.chat_id,
                    reason_event_id=claim.event_id,
                    side_effect=True,
                    tool_name=claim.tool_name,
                    arguments=claim.arguments,
                )
                preflight = await self._policy.evaluate(action)
                if preflight.decision is not PolicyDecision.APPROVE_ONCE:
                    outcome_code = f"policy_{preflight.decision.value}"
                    return
                commit = await self._policy.evaluate(action)
                if commit.decision is not PolicyDecision.APPROVE_ONCE:
                    outcome_code = f"policy_{commit.decision.value}"
                    return
                context = ToolContext(
                    profile=claim.profile,
                    channel=claim.channel,
                    chat_id=claim.chat_id,
                    event_id=claim.event_id,
                    principal_id=event.sender_id,
                    granted_scopes=(
                        self._granted_tool_scopes | manifest.required_scopes
                    ),
                )
                tool_execution_id, created = await self._repository.begin_tool_execution(
                    profile=claim.profile,
                    channel=claim.channel,
                    chat_id=claim.chat_id,
                    event_id=claim.event_id,
                    call_id=f"approval:{claim.approval_id}",
                    tool_name=manifest.name,
                    action_kind=manifest.action_kind,
                    arguments=claim.arguments,
                    side_effect=True,
                )
                if not created or not await self._repository.mark_tool_executing(
                    tool_execution_id
                ):
                    outcome = ApprovalState.UNKNOWN
                    outcome_code = "tool_execution_state_conflict"
                    return
                invocation_started = True
                async with asyncio.timeout(manifest.timeout_seconds):
                    result = await tool.invoke(context, claim.arguments)
                if result.ok and result.side_effect_committed and not result.state_unknown:
                    outcome = ApprovalState.APPROVED
                    outcome_code = "committed"
                    tool_state = "succeeded"
                else:
                    outcome = ApprovalState.UNKNOWN
                    outcome_code = "tool_outcome_unknown"
                    tool_state = "unknown"
                tool_execution_resolved = await self._repository.resolve_tool_execution(
                    tool_execution_id,
                    state=tool_state,
                    outcome_code=outcome_code,
                    result_summary={
                        "ok": result.ok,
                        "content_bytes": len(result.content.encode("utf-8")),
                        "facts": len(result.facts),
                        "artifacts": len(result.artifacts),
                        "side_effect_committed": result.side_effect_committed,
                        "state_unknown": result.state_unknown,
                    },
                )
                if not tool_execution_resolved:
                    outcome = ApprovalState.UNKNOWN
                    outcome_code = "tool_outcome_persistence_failed"
        except asyncio.CancelledError:
            outcome = ApprovalState.UNKNOWN if invocation_started else ApprovalState.DENIED
            outcome_code = "execution_cancelled" if invocation_started else "cancelled_preflight"
            raise
        except BaseException:
            outcome = ApprovalState.UNKNOWN if invocation_started else ApprovalState.DENIED
            outcome_code = "execution_exception" if invocation_started else "preflight_exception"
        finally:
            if tool_execution_id is not None and not tool_execution_resolved:
                try:
                    tool_execution_resolved = await asyncio.shield(
                        self._repository.resolve_tool_execution(
                            tool_execution_id,
                            state="unknown" if invocation_started else "failed",
                            outcome_code=outcome_code,
                        )
                    )
                except BaseException:
                    outcome = ApprovalState.UNKNOWN
                    outcome_code = "tool_outcome_persistence_failed"
            # The claim is terminal after this method, including early returns.
            await asyncio.shield(
                self._approvals.resolve(
                    claim,
                    outcome=outcome,
                    outcome_code=outcome_code,
                )
            )


class LemonbotRuntime:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self.paths = RuntimePaths.from_settings(settings)
        self.database = Database.from_path(self.paths.database)
        self.repository = CoreRepository(self.database)
        self.approval_service = ApprovalService(
            ApprovalRepository(self.database),
            profile=settings.profile,
        )
        self.memory = SQLiteMemoryStore(self.paths.database)
        self.attachments = AttachmentStore(self.paths.database, self.paths.objects)
        self.connector: Connector | None = None
        self.model: ModelBackend | None = None
        self.pipeline: EventPipeline | None = None
        self.proactive_store = ProactiveJobStore(self.paths.database)
        self.proactive_runner: ProactiveRunner | None = None
        self._model_close: Any | None = None
        self._vision_close: Any | None = None
        self._mcp_clients: list[MCPStdioClient] = []
        self._mcp_supervisor = WorkerSupervisor()
        self._budget: PersistentBudgetManager | None = None
        self._emergency = asyncio.Event()
        self._tools: dict[str, Tool] = {}
        self._tool_scopes: frozenset[str] = frozenset()
        self._policy: DeterministicPolicy | None = None
        self._side_effect_lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.paths.ensure()
        await asyncio.to_thread(upgrade_database, self.paths.database)
        await self.database.initialise()
        await self.memory.initialize()
        await self.attachments.initialize()
        await self.proactive_store.initialize()
        # RuntimeLock guarantees this is the only live core for the profile.
        # Every processing/reserved row therefore belongs to the previous
        # process, even if it crashed only milliseconds ago. Startup recovery
        # runs once, so applying a staleness grace period would strand it.
        recovery = await self.repository.recover_interrupted(
            stale_after=timedelta(0)
        )
        recovered_approvals = await self.approval_service.recover_interrupted()
        if recovered_approvals:
            recovery["approvals_unknown"] = recovered_approvals
        if any(recovery.values()):
            logger.warning("recovered interrupted durable states: %s", recovery)
        self.connector = await self._build_connector()
        self.model = await self._build_model()
        tools, scopes = await self._build_tools()
        policy = DeterministicPolicy(self.repository, config=self._policy_config())
        self._tools = dict(tools)
        self._tool_scopes = frozenset(scopes)
        self._policy = policy
        self.pipeline = EventPipeline(
            self.repository,
            policy,
            self.model,
            tools=tools,
            memory_context=MemoryContextService(
                self.memory,
                ContextBuilder(self.model),
            ),
            memory_derivation=MemoryDerivationService(
                store=self.memory,
                backend=self.model,
            ),
            approval_service=self.approval_service,
            side_effect_lock=self._side_effect_lock,
            config=PipelineConfig(
                profile=self.settings.profile,
                welcome_text=(
                    self.settings.wecom.welcome_text
                    if self.settings.runtime.connector == "wecom"
                    else None
                ),
                max_task_seconds=self.settings.limits.event_timeout_seconds,
                max_model_turns=self.settings.limits.max_model_turns,
                max_tool_calls=self.settings.limits.max_tool_calls,
                max_navigations=self.settings.limits.max_navigations,
                max_downloads=self.settings.limits.max_downloads,
                max_reply_chars=(
                    self.settings.limits.max_reply_chunks
                    * self.settings.limits.max_chunk_chars
                ),
                chunk_chars=self.settings.limits.max_chunk_chars,
                model_max_tokens=self.settings.models.max_output_tokens,
                max_context_tokens=self.settings.models.max_input_tokens,
                granted_tool_scopes=self._tool_scopes,
                deep_sender_ids=frozenset(
                    self.settings.wecom.admin_sender_ids
                    if self.settings.runtime.connector == "wecom"
                    else self.settings.wechat_uia.admin_sender_ids
                    if self.settings.runtime.connector == "wechat_uia"
                    else ()
                ),
                output_mode=_pipeline_output_mode(self.settings),
            ),
        )
        self.proactive_runner = ProactiveRunner(
            self.proactive_store,
            self.repository,
            policy,
            self.model,
            max_output_tokens=min(1500, self.settings.models.max_output_tokens),
            side_effect_lock=self._side_effect_lock,
        )
        await self._seed_allowlist()

    def _policy_config(self) -> PolicyConfig:
        limits = self.settings.limits
        return PolicyConfig(
            timezone=self.settings.timezone,
            quiet_start=limits.quiet_start,
            quiet_end=limits.quiet_end,
            wecom=RateLimitProfile(
                reply_per_10_minutes=limits.wecom_reply.per_10_minutes,
                reply_per_hour=limits.wecom_reply.per_hour,
                reply_per_day=limits.wecom_reply.per_day,
                global_per_day=limits.wecom_reply.global_per_day,
                proactive_cooldown_hours=limits.wecom_proactive.period_hours,
                proactive_per_day=limits.wecom_proactive.per_day,
                proactive_global_per_day=limits.wecom_proactive.global_per_day,
                proactive_enabled=True,
            ),
            wechat_lab=RateLimitProfile(
                reply_per_10_minutes=limits.wechat_reply.per_10_minutes,
                reply_per_hour=limits.wechat_reply.per_hour,
                reply_per_day=limits.wechat_reply.per_day,
                global_per_day=limits.wechat_reply.global_per_day,
                proactive_cooldown_hours=limits.wechat_proactive.period_hours,
                proactive_per_day=limits.wechat_proactive.per_day,
                proactive_global_per_day=limits.wechat_proactive.global_per_day,
                proactive_enabled=self.settings.wechat_uia.stage == "proactive",
            ),
        )

    async def _seed_allowlist(self) -> None:
        if self.settings.runtime.connector == "wecom":
            channel, chats = "wecom", self.settings.wecom.allow_chat_ids
        elif self.settings.runtime.connector == "wechat_uia":
            channel, chats = "wechat_personal_lab", self.settings.wechat_uia.allow_chat_ids
        else:
            channel, chats = "fake", ()
        for chat_id in chats:
            await self.repository.set_allowlisted(channel, chat_id, label="config allowlist")

    def _credential_store(self) -> NamespacedSecretStore:
        return NamespacedSecretStore(WindowsCredentialStore(), self.settings.profile)

    async def _build_connector(self) -> Connector:
        selected = self.settings.runtime.connector
        if selected == "fake":
            return FakeConnector(channel="fake")
        secrets = self._credential_store()
        if selected == "wecom":
            async def store_attachment(
                event: InboundEvent,
                content: bytes,
                media_type: str,
                filename: str | None,
            ) -> str:
                stored = await self.attachments.ingest(
                    channel=event.channel,
                    chat_id=event.chat_id,
                    event_id=event.event_id,
                    content=content,
                    media_type=media_type,
                    original_name=filename,
                )
                return str(stored.attachment_id)

            return WeComConnector(
                WeComConfig(
                    bot_id=self.settings.wecom.bot_id,
                    secret=secrets.require("wecom_bot_secret"),
                    allowed_chat_ids=frozenset(self.settings.wecom.allow_chat_ids),
                    welcome_text=self.settings.wecom.welcome_text,
                    max_media_bytes=self.settings.vision.max_file_bytes,
                    max_media_items=self.settings.limits.max_downloads,
                ),
                attachment_sink=store_attachment,
            )
        uia = self.settings.wechat_uia
        backend = None
        if uia.selector_bundle_path:
            bundle = SelectorBundle.load(Path(uia.selector_bundle_path))
            if not set(uia.allow_chat_ids).issubset(bundle.chat_targets):
                raise RuntimeError("UIA allowlist contains a chat absent from the selector bundle")
            backend = WindowsWeChatUIABackend(
                bundle=bundle,
                expected_process_name=uia.expected_process_name,
                expected_executable_path=uia.expected_executable_path or None,
                expected_executable_sha256=uia.expected_executable_sha256 or None,
                expected_windows_user=uia.expected_windows_user or None,
                expected_account_id=uia.expected_account or None,
                enrolled_client_version=uia.enrolled_client_version or None,
                enrolled_selector_signature=uia.enrolled_selector_signature or None,
                poll_seconds=uia.reconcile_seconds,
            )
        return PersonalWeChatConnector(
            PersonalWeChatConfig(
                enabled=uia.enabled,
                stage=PersonalWeChatStage(uia.stage),
                expected_process_name=uia.expected_process_name,
                expected_executable_path=uia.expected_executable_path or None,
                expected_executable_sha256=uia.expected_executable_sha256 or None,
                expected_windows_user=uia.expected_windows_user or None,
                expected_account_id=uia.expected_account or None,
                enrolled_client_version=uia.enrolled_client_version or None,
                enrolled_selector_signature=uia.enrolled_selector_signature or None,
                allowed_chat_ids=frozenset(uia.allow_chat_ids),
            ),
            backend=backend,
        )

    async def _build_model(self) -> ModelBackend:
        if self.settings.models.provider == "fake":
            return FakeModelBackend()
        budget_settings = self.settings.models.budget
        provider = self.settings.models.provider
        local_unmetered = (
            provider == "openai_compatible"
            and (self.settings.models.base_url.host or "").casefold()
            in {"127.0.0.1", "localhost", "::1"}
            and not self.settings.models.api_key_secret_name
        )
        if not budget_settings.enabled and not local_unmetered:
            raise RuntimeError(
                "cloud model budget is disabled; refusing to start an external connector"
            )
        if self.settings.vision.enabled and not budget_settings.enabled:
            raise RuntimeError("cloud vision requires an enabled hard monetary budget")
        config = ProviderConfig(
            provider=provider,
            base_url=str(self.settings.models.base_url),
            secret_name=self.settings.models.api_key_secret_name or None,
            flash_model=self.settings.models.flash_model,
            pro_model=self.settings.models.pro_model,
            timeout_seconds=self.settings.models.request_timeout_seconds,
            context_tokens=self.settings.models.max_input_tokens,
        )
        if local_unmetered and not budget_settings.enabled:
            limits = BudgetLimits(daily=Decimal("1"), monthly=Decimal("1"))
            local_price = ModelPrice(Decimal(0), Decimal(0))
            prices: dict[tuple[str, str], ModelPrice] = {
                (provider, config.flash_model): local_price,
                (provider, config.pro_model): local_price,
            }
        else:
            limits = BudgetLimits(
                daily=budget_settings.daily_limit_cny,
                monthly=budget_settings.monthly_limit_cny,
            )
            prices = {
                (provider, config.flash_model): ModelPrice(
                    budget_settings.flash_input_cny_per_million,
                    budget_settings.flash_output_cny_per_million,
                ),
                (provider, config.pro_model): ModelPrice(
                    budget_settings.pro_input_cny_per_million,
                    budget_settings.pro_output_cny_per_million,
                ),
            }
        if self.settings.vision.enabled:
            vision_input = self.settings.vision.input_cny_per_million
            vision_output = self.settings.vision.output_cny_per_million
            assert vision_input is not None and vision_output is not None
            prices[("zhipu", self.settings.vision.model)] = ModelPrice(
                vision_input,
                vision_output,
            )
        budget = await PersistentBudgetManager.create(
            database_path=self.paths.database,
            limits=limits,
            prices=prices,
            timezone_name=self.settings.timezone,
        )
        self._budget = budget
        backend = await IsolatedModelBackend.create(
            config=ModelWorkerConfig(
                profile=self.settings.profile,
                provider=config,
                verify_models_on_startup=self.settings.models.verify_models_on_startup,
            ),
            budget=budget,
            python_executable=Path(sys.executable),
            cwd=Path(__file__).resolve().parent,
        )
        self._model_close = backend.aclose
        return backend

    async def _build_tools(self) -> tuple[Mapping[str, Tool], set[str]]:
        tools: dict[str, Tool] = {}
        scopes: set[str] = set()
        if self.settings.browser.enabled:
            browser = BrowserReadTool(
                enabled=True,
                max_text_chars=self.settings.browser.max_text_chars,
                timeout_seconds=self.settings.browser.navigation_timeout_seconds,
            )
            tools[browser.manifest().name] = browser
            scopes.add("browser.read_public")
        if self.settings.vision.enabled:
            if self._budget is None:
                raise RuntimeError("vision requires the persistent cloud budget")
            vision_adapter = ZhipuVisionAdapter(
                secret_store=AsyncSecretStoreAdapter(self._credential_store()),
                budget=self._budget,
                config=VisionProviderConfig(
                    base_url=str(self.settings.vision.base_url),
                    model=self.settings.vision.model,
                    image_token_reserve=self.settings.vision.image_token_reserve,
                ),
            )
            vision = ImageUnderstandingTool(
                self.attachments,
                ImagePreprocessor(
                    max_file_bytes=self.settings.vision.max_file_bytes,
                    max_pixels=self.settings.vision.max_pixels,
                ),
                RapidOCRReader(),
                VisionService(vision_adapter),
            )
            tools[vision.manifest().name] = vision
            scopes.add("vision.read")
            self._vision_close = vision_adapter.aclose
        roots: list[VaultRoot] = []
        roots.extend(
            VaultRoot(f"read{index}", Path(path), writable=False)
            for index, path in enumerate(self.settings.vault.read_roots)
        )
        roots.extend(
            VaultRoot(f"write{index}", Path(path), writable=True)
            for index, path in enumerate(self.settings.vault.write_roots)
        )
        if roots:
            vault = FileVault(roots)
            reader = VaultReadTool(vault)
            creator = VaultCreateTool(vault)
            tools[reader.manifest().name] = reader
            tools[creator.manifest().name] = creator
            scopes.add("vault.read")
            # vault.create deliberately requires policy approval and is not
            # placed in automatic scopes.
        started_mcp_clients: list[MCPStdioClient] = []
        try:
            if self.settings.mcp.enabled:
                for configured_server in self.settings.mcp.servers:
                    if not configured_server.enabled:
                        continue
                    server = PinnedMCPServer.model_validate(
                        configured_server.model_dump(mode="python")
                    )
                    client = MCPStdioClient(
                        server,
                        supervisor=self._mcp_supervisor,
                    )
                    # Startup verifies the executable hash, exact protocol/server
                    # version and Job Object assignment before any manifest is exposed.
                    await client.start()
                    started_mcp_clients.append(client)
                    for local_name, configured_tool in server.tools.items():
                        if not configured_tool.enabled:
                            continue
                        adapter = MCPToolAdapter(client, local_name)
                        manifest = adapter.manifest()
                        if manifest.name in tools:
                            raise RuntimeError(f"duplicate enrolled tool: {manifest.name}")
                        tools[manifest.name] = adapter
                        if manifest.action_kind == "mcp_read":
                            scopes.update(manifest.required_scopes)
        except BaseException:
            await asyncio.gather(
                *(client.close() for client in started_mcp_clients),
                return_exceptions=True,
            )
            raise
        self._mcp_clients.extend(started_mcp_clients)
        return tools, scopes

    async def close(self) -> None:
        close_operations: list[Awaitable[Any]] = []
        connector = self.connector
        if connector is not None and hasattr(connector, "close"):
            close_operations.append(connector.close())
        if self._model_close is not None:
            close_operations.append(self._model_close())
        if self._vision_close is not None:
            close_operations.append(self._vision_close())
        clients, self._mcp_clients = self._mcp_clients, []
        close_operations.extend(client.close() for client in clients)
        results = list(
            await asyncio.gather(*close_operations, return_exceptions=True)
        )
        try:
            await self._mcp_supervisor.stop_all()
        except BaseException as exc:
            results.append(exc)
        try:
            await self.database.close()
        except BaseException as exc:
            results.append(exc)
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise RuntimeError("one or more Lemonbot components failed to close") from failures[0]

    async def serve(self, *, enable_tray: bool) -> None:
        if self.pipeline is None or self.connector is None or self._policy is None:
            raise RuntimeError("runtime is not initialized")
        tokens = LocalTokenManager()
        control = RepositoryControl(
            self.repository,
            profile=self.settings.profile,
            connector_name=self.settings.runtime.connector,
            started_at=datetime.now(UTC),
            emergency_event=self._emergency,
            approvals=self.approval_service,
            tools=self._tools,
            policy=self._policy,
            granted_tool_scopes=self._tool_scopes,
            side_effect_lock=self._side_effect_lock,
        )
        app = create_admin_app(
            control,
            tokens,
            host=self.settings.admin.host,
            port=self.settings.admin.port,
        )
        server = uvicorn.Server(
            uvicorn.Config(
                app,
                host=self.settings.admin.host,
                port=self.settings.admin.port,
                access_log=False,
                log_config=None,
            )
        )
        bootstrap = tokens.issue_bootstrap()
        print(
            f"Lemonbot 本地管理台：http://{self.settings.admin.host}:"
            f"{self.settings.admin.port}/login#{bootstrap}"
        )
        loop = asyncio.get_running_loop()
        if enable_tray:
            def emergency_stop_from_tray() -> None:
                asyncio.run_coroutine_threadsafe(control.emergency_stop(), loop)

            def set_pause_from_tray(channel: str | None, paused: bool) -> None:
                asyncio.run_coroutine_threadsafe(
                    control.set_pause(channel, paused), loop
                )

            start_tray(
                tokens,
                host=self.settings.admin.host,
                port=self.settings.admin.port,
                emergency_stop=emergency_stop_from_tray,
                set_pause=set_pause_from_tray,
            )
        tasks = {
            asyncio.create_task(
                self.pipeline.consume_events(self.connector), name="connector-events"
            ),
            asyncio.create_task(self._process_loop(), name="inbox-processor"),
            asyncio.create_task(self._dispatch_loop(), name="outbox-dispatcher"),
            asyncio.create_task(self._proactive_loop(), name="proactive-scheduler"),
            asyncio.create_task(server.serve(), name="local-admin"),
        }
        done, pending = await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        server.should_exit = True
        for task in pending:
            task.cancel()
        await asyncio.gather(*pending, return_exceptions=True)
        for task in done:
            exception = task.exception()
            if exception is not None:
                raise exception

    async def _process_loop(self) -> None:
        assert self.pipeline is not None
        while True:
            result = await self.pipeline.process_once()
            await asyncio.sleep(0.2 if result.status is PipelineStatus.IDLE else 0)

    async def _dispatch_loop(self) -> None:
        assert self.pipeline is not None and self.connector is not None
        channel = (
            "wechat_personal_lab"
            if self.settings.runtime.connector == "wechat_uia"
            else getattr(self.connector, "channel", self.settings.runtime.connector)
        )
        while True:
            result = await self.pipeline.dispatch_once(self.connector, channel=channel)
            await asyncio.sleep(0.2 if result.status is PipelineStatus.IDLE else 0)

    async def _proactive_loop(self) -> None:
        assert self.proactive_runner is not None
        while True:
            handled = await self.proactive_runner.run_once()
            await asyncio.sleep(0 if handled else 2)


async def run_service(settings: AppSettings, *, enable_tray: bool = True) -> None:
    paths = RuntimePaths.from_settings(settings)
    paths.ensure()
    configure_logging(settings.runtime.log_level)
    with RuntimeLock(paths.lock_file):
        runtime = LemonbotRuntime(settings)
        try:
            await runtime.initialize()
            await runtime.serve(enable_tray=enable_tray)
        finally:
            await runtime.close()

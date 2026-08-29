from __future__ import annotations

import asyncio
import hashlib
import logging
import os
import subprocess
import sys
from collections.abc import Awaitable, Mapping
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from pathlib import Path
from typing import Any, Literal

import psutil  # type: ignore[import-untyped]
import uvicorn
from jsonschema import validate  # type: ignore[import-untyped]

from lemonbot.admin import create_admin_app
from lemonbot.admin.auth import LocalTokenManager
from lemonbot.admin.control import ApprovalView, ControlBackend, StatusView
from lemonbot.approvals import ApprovalClaim, ApprovalRepository, ApprovalService
from lemonbot.config import AppSettings, RuntimePaths
from lemonbot.connectors import (
    AtspiEnrollment,
    AtspiObserveConnector,
    FakeConnector,
)
from lemonbot.connectors.atspi_worker_proxy import AtspiWorkerSource
from lemonbot.connectors.wechat_atspi import AtspiCursor
from lemonbot.domain import (
    ApprovalState,
    Connector,
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
    IsolatedVisionBackend,
    ModelPrice,
    ModelWorkerConfig,
    PersistentBudgetManager,
    ProviderConfig,
    VisionProviderConfig,
    VisionWorkerConfig,
)
from lemonbot.orchestration import (
    DisabledModelBackend,
    EventPipeline,
    FakeModelBackend,
    PipelineConfig,
    PipelineStatus,
)
from lemonbot.policy import DeterministicPolicy, PolicyConfig, RateLimitProfile
from lemonbot.proactive import ProactiveJobStore, ProactiveRunner
from lemonbot.runtime_lock import RuntimeLock
from lemonbot.security.redaction import configure_logging
from lemonbot.security.secrets import NamespacedSecretStore, platform_secret_store
from lemonbot.storage import CoreRepository, Database
from lemonbot.storage.migrate import upgrade_database
from lemonbot.supervisor import WorkerSupervisor
from lemonbot.tools import Tool
from lemonbot.tools.attachments import AttachmentStore
from lemonbot.tools.browser_worker_protocol import BrowserWorkerConfig
from lemonbot.tools.browser_worker_proxy import IsolatedBrowserReadTool
from lemonbot.tools.mcp import MCPStdioClient, MCPToolAdapter, PinnedMCPServer
from lemonbot.tools.vault import FileVault, VaultCreateTool, VaultReadTool, VaultRoot
from lemonbot.tools.vision_tool import ImageUnderstandingTool

logger = logging.getLogger(__name__)


def _pipeline_output_mode(settings: AppSettings) -> Literal["observe", "send"]:
    return "observe" if settings.runtime.connector == "wechat_atspi" else "send"


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
        emergency_file: Path | None = None,
        attachment_store: AttachmentStore | None = None,
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
        self._emergency_file = emergency_file
        self._attachment_store = attachment_store

    async def status(self) -> StatusView:
        counts = await self._repository.runtime_counts()
        pending_approvals = await self._approvals.pending()
        attachment_status = (
            self._attachment_store.capacity_status
            if self._attachment_store is not None
            else None
        )
        return StatusView(
            profile=self._profile,
            connector=self._connector_name,
            global_paused=await self._repository.is_paused(),
            channel_pauses={
                "wechat_personal_lab": await self._repository.is_paused(
                    "wechat_personal_lab"
                ),
            },
            emergency_stopped=self._emergency_event.is_set(),
            queue_depth=counts["queue_depth"],
            unknown_outbox=counts["unknown_outbox"],
            pending_approvals=len(pending_approvals),
            attachment_intake_paused=(
                attachment_status.paused if attachment_status is not None else False
            ),
            attachment_capacity_reason=(
                attachment_status.reason if attachment_status is not None else None
            ),
            attachment_free_bytes=(
                attachment_status.last_free_bytes
                if attachment_status is not None
                else None
            ),
            started_at=self._started_at,
        )

    async def set_pause(self, channel: str | None, paused: bool) -> StatusView:
        if self._emergency_event.is_set() and not paused:
            raise RuntimeError("restart is required after emergency stop")
        if channel not in {None, "wechat_personal_lab"}:
            raise ValueError("unknown channel")
        await self._repository.set_paused(channel=channel, paused=paused)
        return await self.status()

    async def emergency_stop(self) -> StatusView:
        self._emergency_event.set()
        if self._emergency_file is not None:
            await asyncio.to_thread(
                self._emergency_file.write_text, "stopped\n", encoding="ascii"
            )
            await asyncio.to_thread(self._emergency_file.chmod, 0o600)
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
                    or not await self._repository.is_allowlisted(claim.channel, claim.chat_id)
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
                    granted_scopes=(self._granted_tool_scopes | manifest.required_scopes),
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
                if not created or not await self._repository.mark_tool_executing(tool_execution_id):
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
        self._browser_close: Any | None = None
        self._mcp_clients: list[MCPStdioClient] = []
        self._worker_supervisor = WorkerSupervisor()
        self._budget: PersistentBudgetManager | None = None
        self._emergency = asyncio.Event()
        self._tools: dict[str, Tool] = {}
        self._tool_scopes: frozenset[str] = frozenset()
        self._policy: DeterministicPolicy | None = None
        self._side_effect_lock = asyncio.Lock()

    async def initialize(self) -> None:
        self.paths.ensure()
        if self.paths.emergency_stop_file.exists():
            self._emergency.set()
            raise RuntimeError(
                "persistent emergency stop is active; run lemonbot resume --confirm"
            )
        await asyncio.to_thread(upgrade_database, self.paths.database)
        await self.database.initialise()
        await self.memory.initialize()
        await self.attachments.initialize()
        observe_only = self.settings.runtime.connector == "wechat_atspi"
        if not observe_only:
            await self.proactive_store.initialize()
        # RuntimeLock guarantees this is the only live core for the profile.
        # Every processing/reserved row therefore belongs to the previous
        # process, even if it crashed only milliseconds ago. Startup recovery
        # runs once, so applying a staleness grace period would strand it.
        recovery = await self.repository.recover_interrupted(stale_after=timedelta(0))
        recovered_approvals = await self.approval_service.recover_interrupted()
        if recovered_approvals:
            recovery["approvals_unknown"] = recovered_approvals
        if any(recovery.values()):
            logger.warning("recovered interrupted durable states: %s", recovery)
        self.connector = await self._build_connector()
        self.model = await self._build_model()
        tools, scopes = ({}, set()) if observe_only else await self._build_tools()
        policy = DeterministicPolicy(self.repository, config=self._policy_config())
        self._tools = dict(tools)
        self._tool_scopes = frozenset(scopes)
        self._policy = policy
        self.pipeline = EventPipeline(
            self.repository,
            policy,
            self.model,
            tools=tools,
            memory_context=(
                None
                if observe_only
                else MemoryContextService(self.memory, ContextBuilder(self.model))
            ),
            memory_derivation=(
                None
                if observe_only
                else MemoryDerivationService(store=self.memory, backend=self.model)
            ),
            approval_service=self.approval_service,
            side_effect_lock=self._side_effect_lock,
            config=PipelineConfig(
                profile=self.settings.profile,
                welcome_text=None,
                max_task_seconds=self.settings.limits.event_timeout_seconds,
                delivery_timeout_seconds=(self.settings.limits.delivery_timeout_seconds),
                max_model_turns=self.settings.limits.max_model_turns,
                max_task_input_tokens=(self.settings.limits.max_task_input_tokens),
                max_task_output_tokens=(self.settings.limits.max_task_output_tokens),
                max_tool_calls=self.settings.limits.max_tool_calls,
                max_navigations=self.settings.limits.max_navigations,
                max_downloads=self.settings.limits.max_downloads,
                max_reply_chars=(
                    self.settings.limits.max_reply_chunks * self.settings.limits.max_chunk_chars
                ),
                chunk_chars=self.settings.limits.max_chunk_chars,
                model_max_tokens=self.settings.models.max_output_tokens,
                max_context_tokens=self.settings.models.max_input_tokens,
                granted_tool_scopes=self._tool_scopes,
                deep_sender_ids=frozenset(),
                output_mode=_pipeline_output_mode(self.settings),
            ),
        )
        if not observe_only:
            self.proactive_runner = ProactiveRunner(
                self.proactive_store,
                self.repository,
                policy,
                self.model,
                max_output_tokens=min(
                    1500,
                    self.settings.models.max_output_tokens,
                    self.settings.limits.max_task_output_tokens,
                ),
                max_input_tokens=self.settings.limits.max_task_input_tokens,
                side_effect_lock=self._side_effect_lock,
            )
        await self._seed_allowlist()

    def _policy_config(self) -> PolicyConfig:
        limits = self.settings.limits
        return PolicyConfig(
            timezone=self.settings.timezone,
            quiet_start=limits.quiet_start,
            quiet_end=limits.quiet_end,
            wechat_lab=RateLimitProfile(
                reply_per_10_minutes=limits.wechat_reply.per_10_minutes,
                reply_per_hour=limits.wechat_reply.per_hour,
                reply_per_day=limits.wechat_reply.per_day,
                global_per_day=limits.wechat_reply.global_per_day,
                proactive_cooldown_hours=limits.wechat_proactive.period_hours,
                proactive_per_day=limits.wechat_proactive.per_day,
                proactive_global_per_day=limits.wechat_proactive.global_per_day,
                proactive_enabled=False,
            ),
        )

    async def _seed_allowlist(self) -> None:
        if self.settings.runtime.connector == "wechat_atspi":
            channel, chats = (
                "wechat_personal_lab",
                self.settings.wechat_atspi.allow_target_refs,
            )
        else:
            channel, chats = "fake", ()
        if channel != "fake":
            await self.repository.reconcile_allowlist(
                channel,
                frozenset(chats),
                label="config allowlist",
            )

    def _credential_store(self) -> NamespacedSecretStore:
        return NamespacedSecretStore(platform_secret_store(), self.settings.profile)

    async def _build_connector(self) -> Connector:
        selected = self.settings.runtime.connector
        if selected == "fake":
            return FakeConnector(channel="fake")
        if selected != "wechat_atspi":
            raise RuntimeError("unsupported connector")
        atspi = self.settings.wechat_atspi
        pids = await asyncio.to_thread(self._verify_linux_wechat)
        enrollment = AtspiEnrollment.load(
            Path(atspi.enrollment_bundle_path),
            atspi.enrollment_bundle_sha256,
        )
        if (
            enrollment.account_fingerprint != atspi.account_fingerprint
            or enrollment.ui_signature != atspi.ui_signature
        ):
            raise RuntimeError("AT-SPI enrollment identity mismatch")
        enrolled_refs = {target.target_ref for target in enrollment.targets}
        allowed_refs = frozenset(atspi.allow_target_refs)
        if not allowed_refs or not allowed_refs <= enrolled_refs:
            raise RuntimeError("AT-SPI allowlist is not contained in the enrollment")
        source = await AtspiWorkerSource.create(
            enrollment=enrollment,
            expected_pids=pids,
            allow_target_refs=allowed_refs,
            debounce_ms=atspi.event_debounce_ms,
            reconcile_seconds=atspi.reconcile_seconds,
            python_executable=Path(atspi.worker_python_path),
            supervisor=self._worker_supervisor,
        )

        async def load_cursor(target_ref: str) -> AtspiCursor | None:
            state = await self.repository.runtime_state(f"atspi:cursor:{target_ref}")
            return None if state is None else AtspiCursor.model_validate(state)

        async def save_cursor(target_ref: str, cursor: AtspiCursor) -> None:
            await self.repository.set_runtime_state(
                f"atspi:cursor:{target_ref}", cursor.model_dump(mode="json")
            )

        return AtspiObserveConnector(
            source,
            enrollment,
            allow_target_refs=allowed_refs,
            load_cursor=load_cursor,
            save_cursor=save_cursor,
        )

    @staticmethod
    def _sha256_file(path: Path) -> str:
        digest = hashlib.sha256()
        with path.open("rb") as stream:
            for chunk in iter(lambda: stream.read(1024 * 1024), b""):
                digest.update(chunk)
        return digest.hexdigest()

    def _verify_linux_wechat(self) -> tuple[int, ...]:
        if not sys.platform.startswith("linux"):
            raise RuntimeError("the AT-SPI connector is Linux-only")
        import pwd

        atspi = self.settings.wechat_atspi
        if not os.environ.get("INVOCATION_ID"):
            raise RuntimeError("the AT-SPI connector must run inside lemonbot.service")
        if os.getuid() != atspi.expected_linux_uid:
            raise RuntimeError("Linux UID differs from enrollment")
        if pwd.getpwuid(os.getuid()).pw_name != atspi.expected_linux_user:
            raise RuntimeError("Linux user differs from enrollment")
        if os.environ.get("XDG_SESSION_TYPE", "").casefold() != atspi.expected_session_type:
            raise RuntimeError("graphical session type differs from enrollment")
        executable = Path(atspi.expected_executable_path)
        worker_python = Path(atspi.worker_python_path)
        if (
            executable.is_symlink()
            or not executable.is_file()
            or not worker_python.is_file()
        ):
            raise RuntimeError("enrolled executable or worker Python is unavailable")
        if self._sha256_file(executable) != atspi.expected_executable_sha256:
            raise RuntimeError("WeChat executable hash differs from enrollment")
        try:
            package = subprocess.run(
                ["/usr/bin/dpkg-query", "-W", "-f=${Version}", "wechat"],
                check=True,
                capture_output=True,
                text=True,
                timeout=10,
            ).stdout.strip()
        except (OSError, subprocess.SubprocessError):
            raise RuntimeError("WeChat package version cannot be verified") from None
        if package.split("-", 1)[0] != atspi.enrolled_client_version:
            raise RuntimeError("WeChat package version differs from enrollment")
        executable_resolved = executable.resolve(strict=True)
        pids: list[int] = []
        for process in psutil.process_iter(("pid", "exe")):
            try:
                process_path = Path(str(process.info.get("exe") or ""))
                if process_path.is_file() and process_path.resolve() == executable_resolved:
                    pids.append(int(process.info["pid"]))
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
        if not pids or len(pids) > 16:
            raise RuntimeError("WeChat process identity is missing or ambiguous")
        return tuple(sorted(pids))

    async def _build_model(self) -> ModelBackend:
        if self.settings.models.provider == "disabled":
            return DisabledModelBackend()
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
            supervisor=self._worker_supervisor,
            python_executable=Path(sys.executable),
            cwd=Path(__file__).resolve().parent,
        )
        self._model_close = backend.aclose
        return backend

    async def _build_tools(self) -> tuple[Mapping[str, Tool], set[str]]:
        tools: dict[str, Tool] = {}
        scopes: set[str] = set()
        if self.settings.browser.enabled:
            browser = await IsolatedBrowserReadTool.create(
                config=BrowserWorkerConfig(
                    max_text_chars=self.settings.browser.max_text_chars,
                    timeout_seconds=(
                        self.settings.browser.navigation_timeout_seconds
                    ),
                ),
                supervisor=self._worker_supervisor,
                python_executable=Path(sys.executable),
                cwd=Path(__file__).resolve().parent,
            )
            tools[browser.manifest().name] = browser
            scopes.add("browser.read_public")
            self._browser_close = browser.aclose
        if self.settings.vision.enabled:
            if self._budget is None:
                raise RuntimeError("vision requires the persistent cloud budget")
            vision_backend = await IsolatedVisionBackend.create(
                budget=self._budget,
                config=VisionWorkerConfig(
                    profile=self.settings.profile,
                    objects_root=str(self.paths.objects.resolve()),
                    provider=VisionProviderConfig(
                        base_url=str(self.settings.vision.base_url),
                        model=self.settings.vision.model,
                        image_token_reserve=(self.settings.vision.image_token_reserve),
                    ),
                    max_file_bytes=self.settings.vision.max_file_bytes,
                    max_pixels=self.settings.vision.max_pixels,
                ),
                supervisor=self._worker_supervisor,
                python_executable=Path(sys.executable),
                cwd=Path(__file__).resolve().parent,
            )
            vision = ImageUnderstandingTool(
                self.attachments,
                isolated_backend=vision_backend,
            )
            tools[vision.manifest().name] = vision
            scopes.add("vision.read")
            self._vision_close = vision_backend.aclose
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
                        supervisor=self._worker_supervisor,
                    )
                    # Startup verifies the executable hash, exact protocol/server
                    # version and supervised-process confinement before any manifest is exposed.
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
        if self._browser_close is not None:
            close_operations.append(self._browser_close())
        clients, self._mcp_clients = self._mcp_clients, []
        close_operations.extend(client.close() for client in clients)
        results = list(await asyncio.gather(*close_operations, return_exceptions=True))
        try:
            await self._worker_supervisor.stop_all()
        except BaseException as exc:
            results.append(exc)
        try:
            await self.database.close()
        except BaseException as exc:
            results.append(exc)
        failures = [result for result in results if isinstance(result, BaseException)]
        if failures:
            raise RuntimeError("one or more Lemonbot components failed to close") from failures[0]

    async def serve(self) -> None:
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
            emergency_file=self.paths.emergency_stop_file,
            attachment_store=self.attachments,
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
        tasks = {
            asyncio.create_task(self._consume_loop(), name="connector-events"),
            asyncio.create_task(self._process_loop(), name="inbox-processor"),
            asyncio.create_task(server.serve(), name="local-admin"),
        }
        if self.settings.runtime.connector != "wechat_atspi":
            tasks.add(asyncio.create_task(self._dispatch_loop(), name="outbox-dispatcher"))
            if self.proactive_runner is not None:
                tasks.add(
                    asyncio.create_task(self._proactive_loop(), name="proactive-scheduler")
                )
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

    async def _consume_loop(self) -> None:
        assert self.pipeline is not None and self.connector is not None
        channel = (
            "wechat_personal_lab"
            if self.settings.runtime.connector == "wechat_atspi"
            else getattr(self.connector, "channel", self.settings.runtime.connector)
        )
        async for event in self.connector.events():
            if self._emergency.is_set() or await self.repository.is_paused(channel):
                continue
            await self.pipeline.ingest(event)

    async def _dispatch_loop(self) -> None:
        assert self.pipeline is not None and self.connector is not None
        channel = (
            "wechat_personal_lab"
            if self.settings.runtime.connector == "wechat_atspi"
            else getattr(self.connector, "channel", self.settings.runtime.connector)
        )
        while True:
            if await self.repository.is_paused(channel):
                await asyncio.sleep(1)
                continue
            result = await self.pipeline.dispatch_once(self.connector, channel=channel)
            await asyncio.sleep(1 if result.status is PipelineStatus.IDLE else 0)

    async def _proactive_loop(self) -> None:
        assert self.proactive_runner is not None
        while True:
            handled = await self.proactive_runner.run_once()
            await asyncio.sleep(0 if handled else 2)


async def run_service(settings: AppSettings) -> None:
    paths = RuntimePaths.from_settings(settings)
    paths.ensure()
    configure_logging(settings.runtime.log_level)
    with RuntimeLock(paths.lock_file):
        runtime = LemonbotRuntime(settings)
        try:
            await runtime.initialize()
            await runtime.serve()
        finally:
            await runtime.close()

from __future__ import annotations

import asyncio
import getpass
import json
import os
import subprocess
import sys
import tempfile
from datetime import UTC, datetime
from pathlib import Path
from typing import Annotated

import psutil  # type: ignore[import-untyped]
import typer
from pydantic import ValidationError

from lemonbot.backup import BackupError, create_backup, restore_backup
from lemonbot.config import RuntimePaths, load_settings
from lemonbot.config.settings import default_config_path
from lemonbot.data import DataOperationError, delete_conversation, export_profile_data
from lemonbot.doctor import run_checks
from lemonbot.domain import InboundEvent
from lemonbot.orchestration import EventPipeline, FakeModelBackend
from lemonbot.policy import DeterministicPolicy
from lemonbot.proactive import JobSource, ProactiveJob, ProactiveJobStore
from lemonbot.runtime_lock import AlreadyRunningError, RuntimeLock
from lemonbot.security.secrets import (
    NamespacedSecretStore,
    SecretStoreError,
    platform_secret_store,
)

app = typer.Typer(no_args_is_help=True, help="Lemonbot 2026 local runtime")
secret_app = typer.Typer(no_args_is_help=True, help="Manage platform credential-store entries")
schedule_app = typer.Typer(no_args_is_help=True, help="Manage bounded proactive jobs")
uia_app = typer.Typer(
    no_args_is_help=True,
    help="Inspect personal WeChat UIA enrollment and advance durable stage gates",
)
data_app = typer.Typer(no_args_is_help=True, help="Offline administrator data operations")
outbox_app = typer.Typer(no_args_is_help=True, help="Reconcile ambiguous outbound sends")
channel_app = typer.Typer(no_args_is_help=True, help="Inspect and manage chat channels")
app.add_typer(secret_app, name="secret")
app.add_typer(schedule_app, name="schedule")
app.add_typer(uia_app, name="uia")
app.add_typer(data_app, name="data")
app.add_typer(outbox_app, name="outbox")
app.add_typer(channel_app, name="channel")

ConfigOption = Annotated[
    Path | None,
    typer.Option("--config", help="Path to a non-secret TOML configuration file"),
]
OutputOption = Annotated[Path | None, typer.Option("--output")]
PromptOption = Annotated[str | None, typer.Option("--prompt")]
SelectorBundleOption = Annotated[
    Path | None,
    typer.Option(
        "--selector-bundle",
        help="Override the selector bundle path from config for read-only enrollment",
    ),
]
LinuxProbePidOption = Annotated[
    list[int] | None,
    typer.Option("--pid", help="Exact WeChat PID; repeatable"),
]
LinuxProbeMaxNodesOption = Annotated[
    int,
    typer.Option("--max-nodes", min=100, max=20_000),
]

_SECRET_NAMES = {"deepseek_api_key", "zhipu_api_key", "wecom_bot_secret"}


@channel_app.command("linux-atspi-probe")
def linux_atspi_probe(
    pid: LinuxProbePidOption = None,
    max_nodes: LinuxProbeMaxNodesOption = 10_000,
) -> None:
    """Run the sanitized, read-only AT-SPI probe with system Python."""
    if not sys.platform.startswith("linux"):
        typer.echo("Linux AT-SPI 探针只能在 Linux 图形会话中运行。", err=True)
        raise typer.Exit(2)

    target_pids = set(pid or [])
    if not target_pids:
        for process in psutil.process_iter(("pid", "name", "exe")):
            try:
                name = str(process.info.get("name") or "").casefold()
                executable = str(process.info.get("exe") or "")
            except (psutil.AccessDenied, psutil.NoSuchProcess):
                continue
            if name in {"wechat", "weixin"} or executable == "/opt/wechat/wechat":
                target_pids.add(int(process.info["pid"]))
    if not target_pids or len(target_pids) > 16 or any(value <= 0 for value in target_pids):
        typer.echo("未找到唯一可审计的微信进程集合；请使用 --pid 指定。", err=True)
        raise typer.Exit(2)

    system_python = Path("/usr/bin/python3")
    helper = Path(__file__).with_name("connectors") / "linux_atspi_probe.py"
    if not system_python.is_file() or not helper.is_file():
        typer.echo("系统 Python 或 AT-SPI 探针文件不可用。", err=True)
        raise typer.Exit(1)
    command = [str(system_python), "-I", str(helper)]
    for target_pid in sorted(target_pids):
        command.extend(("--pid", str(target_pid)))
    command.extend(("--max-nodes", str(max_nodes)))
    allowed_environment = {
        key: value
        for key, value in os.environ.items()
        if key
        in {
            "DBUS_SESSION_BUS_ADDRESS",
            "DISPLAY",
            "LANG",
            "LC_ALL",
            "WAYLAND_DISPLAY",
            "XDG_RUNTIME_DIR",
        }
    }
    try:
        completed = subprocess.run(  # noqa: S603
            command,
            check=False,
            capture_output=True,
            env=allowed_environment,
            timeout=30,
        )
        report = json.loads(completed.stdout.decode("utf-8"))
        if completed.returncode != 0 or not isinstance(report, dict):
            raise ValueError("probe failed")
    except (OSError, subprocess.SubprocessError, UnicodeError, json.JSONDecodeError, ValueError):
        typer.echo("Linux AT-SPI 只读探测失败（输出已隐藏）。", err=True)
        raise typer.Exit(1) from None
    typer.echo(json.dumps(report, ensure_ascii=True, sort_keys=True))


def _settings(config: Path | None):  # type: ignore[no-untyped-def]
    path = config or default_config_path()
    try:
        return load_settings(path)
    except FileNotFoundError:
        typer.echo(f"配置文件不存在：{path}", err=True)
        typer.echo("请复制 config/lemonbot.example.toml 并按需修改（不要写入密钥）。", err=True)
        raise typer.Exit(2) from None
    except ValidationError as exc:
        typer.echo("配置无效：", err=True)
        for issue in exc.errors(
            include_url=False,
            include_context=False,
            include_input=False,
        ):
            location = ".".join(str(part) for part in issue["loc"])
            typer.echo(f"- {location or '<root>'}: {issue['msg']}", err=True)
        raise typer.Exit(2) from None
    except Exception as exc:
        # Parsing and filesystem exceptions can echo source lines or path
        # fragments.  Never reflect arbitrary configuration contents.
        typer.echo(f"配置无效（{type(exc).__name__}）。", err=True)
        raise typer.Exit(2) from None


@uia_app.command("inspect")
def uia_inspect(
    config: ConfigOption = None,
    selector_bundle: SelectorBundleOption = None,
) -> None:
    """Inspect enrolled UIA identity without emitting account or chat text."""
    settings = _settings(config)
    if settings.profile != "lab":
        typer.echo("UIA 检查只允许 lab profile。", err=True)
        raise typer.Exit(2)
    configured_path = settings.wechat_uia.selector_bundle_path
    bundle_path = selector_bundle or (Path(configured_path) if configured_path else None)
    if bundle_path is None:
        typer.echo("请通过 --selector-bundle 或配置指定 selector bundle。", err=True)
        raise typer.Exit(2)

    async def inspect() -> dict[str, str | int | bool | None]:
        from lemonbot.connectors import SelectorBundle, WindowsWeChatUIABackend

        bundle = SelectorBundle.load(bundle_path)
        backend = WindowsWeChatUIABackend(
            bundle=bundle,
            expected_process_name=settings.wechat_uia.expected_process_name,
            poll_seconds=settings.wechat_uia.reconcile_seconds,
        )
        try:
            snapshot = await backend.inspect()
        finally:
            await backend.close()
        return {
            "account_sha256": snapshot.account_id,
            "executable_path": snapshot.executable_path,
            "executable_sha256": snapshot.executable_sha256,
            "selector_sha256": snapshot.selector_signature,
            "client_version": snapshot.client_version,
            "window_handle": snapshot.window_handle,
            "session_locked": snapshot.session_locked,
        }

    try:
        result = asyncio.run(inspect())
    except Exception as exc:
        # Pydantic/UIA exception details may contain enrolled display strings.
        typer.echo(f"UIA 只读检查失败：{type(exc).__name__}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(result, ensure_ascii=True, sort_keys=True))


@uia_app.command("pywechat-probe")
def uia_pywechat_probe(
    process_name: str = typer.Option(
        "Weixin.exe",
        "--process-name",
        help="Weixin.exe for WeChat 4.x; WeChat.exe for older clients",
    ),
    max_nodes: int = typer.Option(5_000, "--max-nodes", min=100, max=20_000),
) -> None:
    """Run a sanitized, read-only probe using audited pywechat 4.x selectors."""
    try:
        from lemonbot.connectors.pywechat_probe import probe_pywechat_surface

        report = probe_pywechat_surface(
            expected_process_name=process_name,
            max_nodes=max_nodes,
        )
    except Exception as exc:
        # Never echo UIA exception text: it can include contact or message text.
        typer.echo(f"pywechat 只读探测失败：{type(exc).__name__}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(json.dumps(report.safe_dict(), ensure_ascii=True, sort_keys=True))


@uia_app.command("promote")
def uia_promote(
    target: str = typer.Option(..., "--to", help="Next stage: draft, reply, or proactive"),
    config: ConfigOption = None,
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    """Promote the lab UIA gate by exactly one stage after a live preflight."""
    if not confirm:
        typer.echo("晋级会扩大个人微信自动化能力；核对配置后添加 --confirm。", err=True)
        raise typer.Exit(2)
    if target not in {"draft", "reply", "proactive"}:
        typer.echo("--to 必须是 draft、reply 或 proactive。", err=True)
        raise typer.Exit(2)
    settings = _settings(config)
    uia = settings.wechat_uia
    if settings.profile != "lab" or settings.runtime.connector != "wechat_uia":
        typer.echo("UIA 晋级只允许 lab profile 的 wechat_uia connector。", err=True)
        raise typer.Exit(2)
    if not uia.enabled or uia.stage != target:
        typer.echo(
            "请先启用 UIA，并把配置中的 wechat_uia.stage 精确改为目标阶段。",
            err=True,
        )
        raise typer.Exit(2)

    async def verify_and_promote() -> str:
        from lemonbot.connectors import (
            PersonalWeChatConfig,
            PersonalWeChatConnector,
            PersonalWeChatStage,
            SelectorBundle,
            WindowsWeChatUIABackend,
            promote_uia_stage,
        )
        from lemonbot.storage import CoreRepository, Database
        from lemonbot.storage.migrate import upgrade_database

        bundle = SelectorBundle.load(Path(uia.selector_bundle_path))
        if not set(uia.allow_chat_ids).issubset(bundle.chat_targets):
            raise ValueError("UIA allowlist contains a chat absent from the selector bundle")
        backend = WindowsWeChatUIABackend(
            bundle=bundle,
            expected_process_name=uia.expected_process_name,
            expected_executable_path=uia.expected_executable_path,
            expected_executable_sha256=uia.expected_executable_sha256,
            expected_windows_user=uia.expected_windows_user,
            expected_account_id=uia.expected_account,
            enrolled_client_version=uia.enrolled_client_version,
            enrolled_selector_signature=uia.enrolled_selector_signature,
            poll_seconds=uia.reconcile_seconds,
        )
        connector = PersonalWeChatConnector(
            PersonalWeChatConfig(
                enabled=True,
                stage=PersonalWeChatStage.OBSERVE,
                expected_process_name=uia.expected_process_name,
                expected_executable_path=uia.expected_executable_path,
                expected_executable_sha256=uia.expected_executable_sha256,
                expected_windows_user=uia.expected_windows_user,
                expected_account_id=uia.expected_account,
                enrolled_client_version=uia.enrolled_client_version,
                enrolled_selector_signature=uia.enrolled_selector_signature,
                allowed_chat_ids=frozenset(uia.allow_chat_ids),
            ),
            backend=backend,
        )
        try:
            report = await connector.preflight()
            if not report.safe:
                raise RuntimeError("live UIA preflight did not prove the enrolled identity")
            paths = RuntimePaths.from_settings(settings)
            await asyncio.to_thread(upgrade_database, paths.database)
            database = Database.from_path(paths.database)
            await database.initialise()
            try:
                promoted = await promote_uia_stage(
                    CoreRepository(database),
                    uia,
                    target,  # type: ignore[arg-type]
                )
            finally:
                await database.close()
            return promoted
        finally:
            await connector.close()

    paths = RuntimePaths.from_settings(settings)
    paths.ensure()
    try:
        with RuntimeLock(paths.lock_file):
            promoted = asyncio.run(verify_and_promote())
    except (AlreadyRunningError, RuntimeError, ValueError, OSError) as exc:
        typer.echo(f"UIA 晋级失败：{type(exc).__name__}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"UIA 已晋级到 {promoted}；启动时仍会重复执行身份和目标校验。")


@data_app.command("export")
def data_export_command(
    config: ConfigOption = None,
    output: OutputOption = None,
) -> None:
    """Export the offline current profile in safe backup-format v1."""
    settings = _settings(config)
    try:
        archive = export_profile_data(RuntimePaths.from_settings(settings), output)
    except (AlreadyRunningError, BackupError, DataOperationError) as exc:
        typer.echo(f"数据导出失败：{exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(str(archive))


@data_app.command("delete-conversation")
def data_delete_conversation(
    channel: str,
    chat_id: str,
    config: ConfigOption = None,
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    """Permanently delete one exact conversation from an offline profile."""
    if not confirm:
        typer.echo(
            "这是不可逆操作；停止 Lemonbot，确认目标 profile 后添加 --confirm。",
            err=True,
        )
        raise typer.Exit(2)
    settings = _settings(config)
    configured_chat_ids: tuple[str, ...] = ()
    if channel == "wecom":
        configured_chat_ids = settings.wecom.allow_chat_ids
    elif channel == "wechat_personal_lab":
        configured_chat_ids = settings.wechat_uia.allow_chat_ids
    if chat_id in configured_chat_ids:
        typer.echo(
            "目标仍在配置白名单中；请先从配置和 UIA selector 映射移除，再执行删除。",
            err=True,
        )
        raise typer.Exit(2)
    try:
        result = delete_conversation(
            RuntimePaths.from_settings(settings),
            channel=channel,
            chat_id=chat_id,
        )
    except (AlreadyRunningError, DataOperationError) as exc:
        typer.echo(f"会话删除失败：{exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(
        json.dumps(
            {
                "operation_id": result.operation_id,
                "rows_deleted": result.total_rows,
                "objects_removed": result.objects_removed,
                "object_cleanup_failures": result.object_cleanup_failures,
            },
            ensure_ascii=True,
            sort_keys=True,
        )
    )
    if result.object_cleanup_failures:
        typer.echo("数据库删除已提交，但部分零引用对象未能清理；请离线人工核对。", err=True)
        raise typer.Exit(1)


@outbox_app.command("unknown")
def outbox_unknown(
    config: ConfigOption = None,
    limit: int = typer.Option(100, "--limit", min=1, max=500),
) -> None:
    """List ambiguous sends without printing message bodies."""
    settings = _settings(config)
    paths = RuntimePaths.from_settings(settings)
    paths.ensure()

    async def list_items() -> list[dict[str, object]]:
        from lemonbot.storage import CoreRepository, Database
        from lemonbot.storage.migrate import upgrade_database

        await asyncio.to_thread(upgrade_database, paths.database)
        database = Database.from_path(paths.database)
        await database.initialise()
        try:
            return await CoreRepository(database).list_unknown_outbox(limit=limit)
        finally:
            await database.close()

    try:
        with RuntimeLock(paths.lock_file):
            items = asyncio.run(list_items())
    except (AlreadyRunningError, RuntimeError, OSError) as exc:
        typer.echo(f"读取 unknown outbox 失败：{type(exc).__name__}", err=True)
        raise typer.Exit(1) from exc
    for item in items:
        typer.echo(json.dumps(item, ensure_ascii=True, sort_keys=True))
    if not items:
        typer.echo("没有 unknown outbox。")


@outbox_app.command("resolve")
def outbox_resolve(
    item_id: int,
    outcome: str = typer.Option(..., "--as", help="acknowledged or dead"),
    note: str = typer.Option(..., "--note", help="Manual inspection evidence"),
    config: ConfigOption = None,
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    """Resolve one ambiguous send after manually checking the target chat."""
    if not confirm or outcome not in {"acknowledged", "dead"}:
        typer.echo(
            "人工核对目标会话后，指定 --as acknowledged|dead、--note 和 --confirm。",
            err=True,
        )
        raise typer.Exit(2)
    settings = _settings(config)
    paths = RuntimePaths.from_settings(settings)
    paths.ensure()

    async def reconcile() -> bool:
        from lemonbot.storage import CoreRepository, Database
        from lemonbot.storage.migrate import upgrade_database

        await asyncio.to_thread(upgrade_database, paths.database)
        database = Database.from_path(paths.database)
        await database.initialise()
        try:
            return await CoreRepository(database).reconcile_unknown_outbox(
                item_id,
                outcome=outcome,
                operator_note=note,
            )
        finally:
            await database.close()

    try:
        with RuntimeLock(paths.lock_file):
            resolved = asyncio.run(reconcile())
    except (AlreadyRunningError, RuntimeError, ValueError, OSError) as exc:
        typer.echo(f"outbox 核对失败：{type(exc).__name__}", err=True)
        raise typer.Exit(1) from exc
    if not resolved:
        typer.echo("该记录不存在或已不处于 unknown；未做更改。", err=True)
        raise typer.Exit(1)
    typer.echo(f"outbox {item_id} 已人工核对为 {outcome}，不会自动重发。")


@app.command()
def doctor(config: ConfigOption = None) -> None:
    """Run local, non-mutating deployment checks."""
    settings = _settings(config)
    paths = RuntimePaths.from_settings(settings)
    checks = run_checks(settings, paths)
    for check in checks:
        marker = "OK" if check.ok else ("WARN" if not check.required else "FAIL")
        typer.echo(f"[{marker:4}] {check.name}: {check.detail}")
    if any(not item.ok and item.required for item in checks):
        raise typer.Exit(1)


@app.command()
def run(config: ConfigOption = None, no_tray: bool = typer.Option(False, "--no-tray")) -> None:
    """Start the core, configured connector and loopback administration server."""
    settings = _settings(config)
    from lemonbot.runtime import run_service

    try:
        asyncio.run(run_service(settings, enable_tray=not no_tray))
    except KeyboardInterrupt:
        typer.echo("Lemonbot 已停止。")


@app.command()
def smoke() -> None:
    """Run an offline fake event through the durable pipeline."""

    async def scenario(root: Path) -> str:
        from lemonbot.connectors import FakeConnector
        from lemonbot.storage import CoreRepository, Database

        database = Database.from_path(root / "smoke.db")
        await database.initialise()
        repository = CoreRepository(database)
        await repository.set_allowlisted("fake", "smoke-chat", label="offline smoke")
        connector = FakeConnector(channel="fake")
        pipeline = EventPipeline(
            repository,
            DeterministicPolicy(repository),
            FakeModelBackend(["Lemonbot fake vertical slice is healthy."]),
        )
        try:
            await pipeline.ingest(
                InboundEvent(
                    channel="fake",
                    event_id=f"smoke-{datetime.now(UTC).timestamp()}",
                    chat_id="smoke-chat",
                    sender_id="local-operator",
                    text="health check",
                )
            )
            await pipeline.process_once("fake")
            result = await pipeline.dispatch_once(connector, channel="fake")
            if result.status.value != "acknowledged":
                raise RuntimeError(f"unexpected smoke result: {result.status.value}")
            return connector.delivered_messages[0].text
        finally:
            await database.close()

    with tempfile.TemporaryDirectory(prefix="lemonbot-smoke-") as temporary:
        typer.echo(asyncio.run(scenario(Path(temporary))))


@app.command("backup")
def backup_command(
    config: ConfigOption = None,
    output: OutputOption = None,
) -> None:
    """Create a consistent database and object-store backup."""
    settings = _settings(config)
    try:
        result = create_backup(RuntimePaths.from_settings(settings), output)
    except BackupError as exc:
        typer.echo(f"备份失败：{exc}", err=True)
        raise typer.Exit(1) from exc
    typer.echo(str(result))


@app.command("restore")
def restore_command(
    archive: Path,
    config: ConfigOption = None,
    confirm: bool = typer.Option(False, "--confirm", help="Confirm replacement of the active DB"),
) -> None:
    """Restore an offline profile, preserving the current state as a backup."""
    if not confirm:
        typer.echo("恢复会替换当前数据库；请停止 Lemonbot 并添加 --confirm。", err=True)
        raise typer.Exit(2)
    settings = _settings(config)
    try:
        preserved = restore_backup(RuntimePaths.from_settings(settings), archive)
    except BackupError as exc:
        typer.echo(f"恢复失败：{exc}", err=True)
        raise typer.Exit(1) from exc
    if preserved:
        typer.echo(f"恢复完成；原状态已保存在 {preserved}")
    else:
        typer.echo("恢复完成。")


@app.command("install-startup")
def install_startup(config: ConfigOption = None) -> None:
    """Install a per-user Windows logon task for this profile."""
    if sys.platform != "win32":
        typer.echo("启动任务只支持 Windows。", err=True)
        raise typer.Exit(2)
    settings = _settings(config)
    config_path = (config or default_config_path()).resolve(strict=True)
    task_name = f"Lemonbot-{settings.profile}"
    command = f'"{sys.executable}" -m lemonbot run --config "{config_path}"'
    try:
        task_scheduler = Path(os.environ["SystemRoot"]) / "System32" / "schtasks.exe"
        subprocess.run(  # noqa: S603 - fixed Windows binary and argument vector
            [
                str(task_scheduler),
                "/Create",
                "/SC",
                "ONLOGON",
                "/TN",
                task_name,
                "/TR",
                command,
                "/F",
            ],
            check=True,
        )
    except subprocess.CalledProcessError as exc:
        typer.echo(f"创建启动任务失败（exit={exc.returncode}）。", err=True)
        raise typer.Exit(1) from exc
    typer.echo(f"已安装当前用户启动任务：{task_name}")


@secret_app.command("set")
def secret_set(
    name: str,
    profile: str = typer.Option("prod", "--profile"),
) -> None:
    if name not in _SECRET_NAMES or profile not in {"prod", "lab"}:
        typer.echo("未知的密钥名称或 profile。", err=True)
        raise typer.Exit(2)
    value = getpass.getpass(f"输入 {profile}/{name}（不会回显）: ")
    confirmation = getpass.getpass("再次输入: ")
    if value != confirmation:
        typer.echo("两次输入不一致。", err=True)
        raise typer.Exit(2)
    try:
        NamespacedSecretStore(platform_secret_store(), profile).set(name, value)
    except SecretStoreError:
        typer.echo("安全凭据存储不可用或仍处于锁定状态。", err=True)
        raise typer.Exit(1) from None
    typer.echo("密钥已写入系统安全凭据存储。")


@secret_app.command("status")
def secret_status(profile: str = typer.Option("prod", "--profile")) -> None:
    if profile not in {"prod", "lab"}:
        raise typer.BadParameter("profile must be prod or lab")
    try:
        store = NamespacedSecretStore(platform_secret_store(), profile)
        statuses = {name: store.get(name) is not None for name in sorted(_SECRET_NAMES)}
    except SecretStoreError:
        typer.echo("安全凭据存储不可用或仍处于锁定状态。", err=True)
        raise typer.Exit(1) from None
    for name, configured in statuses.items():
        typer.echo(f"{name}: {'configured' if configured else 'missing'}")


@secret_app.command("delete")
def secret_delete(
    name: str,
    profile: str = typer.Option("prod", "--profile"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    if name not in _SECRET_NAMES or profile not in {"prod", "lab"} or not confirm:
        typer.echo("请指定有效名称/profile，并用 --confirm 确认删除凭据。", err=True)
        raise typer.Exit(2)
    try:
        deleted = NamespacedSecretStore(platform_secret_store(), profile).delete(name)
    except SecretStoreError:
        typer.echo("安全凭据存储不可用或仍处于锁定状态。", err=True)
        raise typer.Exit(1) from None
    typer.echo("已删除。" if deleted else "该凭据不存在。")


@schedule_app.command("add")
def schedule_add(
    chat_id: str,
    reason_event_id: str,
    due_at: str,
    config: ConfigOption = None,
    prompt: PromptOption = None,
    source: JobSource = JobSource.ADMIN_SCHEDULE,
    every_hours: int | None = typer.Option(None, "--every-hours", min=1),
) -> None:
    settings = _settings(config)
    channel = "wecom" if settings.profile == "prod" else "wechat_personal_lab"
    allowed = (
        settings.wecom.allow_chat_ids
        if settings.profile == "prod"
        else settings.wechat_uia.allow_chat_ids
    )
    if chat_id not in allowed:
        typer.echo("目标会话不在当前 profile 的配置白名单中。", err=True)
        raise typer.Exit(2)
    try:
        due = datetime.fromisoformat(due_at.replace("Z", "+00:00"))
    except ValueError as exc:
        raise typer.BadParameter("due_at must be ISO-8601") from exc
    if due.tzinfo is None:
        raise typer.BadParameter("due_at must include a timezone")
    text = prompt or typer.prompt("主动消息任务（内容将作为不可信数据交给模型）")
    job = ProactiveJob(
        source=source,
        channel=channel,
        chat_id=chat_id,
        reason_event_id=reason_event_id,
        prompt=text,
        due_at=due,
        recurrence_seconds=every_hours * 3600 if every_hours is not None else None,
    )

    async def add_job() -> None:
        from lemonbot.storage import CoreRepository, Database

        database_path = RuntimePaths.from_settings(settings).database
        database = Database.from_path(database_path)
        await database.initialise()
        try:
            repository = CoreRepository(database)
            if not await repository.has_inbound_event(channel, chat_id, reason_event_id):
                raise ValueError(
                    "reason_event_id does not identify an event in this exact conversation"
                )
            store = ProactiveJobStore(database_path)
            await store.initialize()
            await store.add(job)
        finally:
            await database.close()

    try:
        asyncio.run(add_job())
    except ValueError as exc:
        typer.echo(f"拒绝创建任务：{exc}", err=True)
        raise typer.Exit(2) from None
    typer.echo(str(job.job_id))


@schedule_app.command("list")
def schedule_list(config: ConfigOption = None) -> None:
    settings = _settings(config)

    async def list_jobs() -> tuple[ProactiveJob, ...]:
        store = ProactiveJobStore(RuntimePaths.from_settings(settings).database)
        await store.initialize()
        return await store.list()

    for job in asyncio.run(list_jobs()):
        typer.echo(
            f"{job.job_id} {job.status.value} {job.due_at.isoformat()} "
            f"{job.channel}/{job.chat_id} {job.source.value}"
        )


@schedule_app.command("cancel")
def schedule_cancel(
    job_id: str,
    config: ConfigOption = None,
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    if not confirm:
        typer.echo("添加 --confirm 以取消任务。", err=True)
        raise typer.Exit(2)
    settings = _settings(config)

    async def cancel_job() -> bool:
        from uuid import UUID

        store = ProactiveJobStore(RuntimePaths.from_settings(settings).database)
        await store.initialize()
        return await store.cancel(UUID(job_id))

    typer.echo("已取消。" if asyncio.run(cancel_job()) else "任务不存在或已结束。")

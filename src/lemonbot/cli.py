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
from lemonbot.runtime_lock import AlreadyRunningError
from lemonbot.security.secrets import NamespacedSecretStore, WindowsCredentialStore

app = typer.Typer(no_args_is_help=True, help="Lemonbot 2026 local runtime")
secret_app = typer.Typer(no_args_is_help=True, help="Manage Windows Credential Manager entries")
schedule_app = typer.Typer(no_args_is_help=True, help="Manage bounded proactive jobs")
uia_app = typer.Typer(no_args_is_help=True, help="Read-only personal WeChat UIA enrollment")
data_app = typer.Typer(no_args_is_help=True, help="Offline administrator data operations")
app.add_typer(secret_app, name="secret")
app.add_typer(schedule_app, name="schedule")
app.add_typer(uia_app, name="uia")
app.add_typer(data_app, name="data")

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

_SECRET_NAMES = {"deepseek_api_key", "zhipu_api_key", "wecom_bot_secret"}


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
    NamespacedSecretStore(WindowsCredentialStore(), profile).set(name, value)
    typer.echo("密钥已写入 Windows Credential Manager。")


@secret_app.command("status")
def secret_status(profile: str = typer.Option("prod", "--profile")) -> None:
    if profile not in {"prod", "lab"}:
        raise typer.BadParameter("profile must be prod or lab")
    store = NamespacedSecretStore(WindowsCredentialStore(), profile)
    for name in sorted(_SECRET_NAMES):
        typer.echo(f"{name}: {'configured' if store.get(name) is not None else 'missing'}")


@secret_app.command("delete")
def secret_delete(
    name: str,
    profile: str = typer.Option("prod", "--profile"),
    confirm: bool = typer.Option(False, "--confirm"),
) -> None:
    if name not in _SECRET_NAMES or profile not in {"prod", "lab"} or not confirm:
        typer.echo("请指定有效名称/profile，并用 --confirm 确认删除凭据。", err=True)
        raise typer.Exit(2)
    deleted = NamespacedSecretStore(WindowsCredentialStore(), profile).delete(name)
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

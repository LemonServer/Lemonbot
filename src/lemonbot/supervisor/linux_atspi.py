"""Linux-only launcher for a narrowly filtered AT-SPI worker."""

from __future__ import annotations

import ast
import asyncio
import os
import re
import shutil
import sys
from dataclasses import dataclass
from pathlib import Path
from uuid import uuid4

from .processes import WorkerProcess, WorkerSupervisor


class LinuxSandboxError(RuntimeError):
    pass


async def _command(*arguments: str, timeout_seconds: float = 10) -> str:
    process = await asyncio.create_subprocess_exec(
        *arguments,
        stdin=asyncio.subprocess.DEVNULL,
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.DEVNULL,
    )
    try:
        stdout, _ = await asyncio.wait_for(process.communicate(), timeout_seconds)
    except TimeoutError:
        process.kill()
        await process.wait()
        raise LinuxSandboxError("D-Bus identity command timed out") from None
    if process.returncode != 0:
        raise LinuxSandboxError("D-Bus identity command failed")
    return stdout.decode("utf-8", errors="strict").strip()


async def _atspi_bus_address(gdbus: str) -> str:
    configured = os.environ.get("AT_SPI_BUS_ADDRESS", "").strip()
    if configured.startswith("unix:path="):
        return configured
    output = await _command(
        gdbus,
        "call",
        "--session",
        "--dest",
        "org.a11y.Bus",
        "--object-path",
        "/org/a11y/bus",
        "--method",
        "org.a11y.Bus.GetAddress",
    )
    try:
        value = ast.literal_eval(output)
        address = str(value[0])
    except (ValueError, SyntaxError, IndexError, TypeError):
        raise LinuxSandboxError("AT-SPI bus address was malformed") from None
    if not address.startswith("unix:path="):
        raise LinuxSandboxError("AT-SPI bus is not a local Unix socket")
    return address


async def _wechat_bus_names(gdbus: str, address: str, pids: frozenset[int]) -> tuple[str, ...]:
    listing = await _command(
        gdbus,
        "call",
        "--address",
        address,
        "--dest",
        "org.freedesktop.DBus",
        "--object-path",
        "/org/freedesktop/DBus",
        "--method",
        "org.freedesktop.DBus.ListNames",
    )
    try:
        parsed = ast.literal_eval(listing)
        names = tuple(str(name) for name in parsed[0] if str(name).startswith(":"))
    except (ValueError, SyntaxError, IndexError, TypeError):
        raise LinuxSandboxError("AT-SPI bus name list was malformed") from None
    matches: list[str] = []
    for name in names:
        result = await _command(
            gdbus,
            "call",
            "--address",
            address,
            "--dest",
            "org.freedesktop.DBus",
            "--object-path",
            "/org/freedesktop/DBus",
            "--method",
            "org.freedesktop.DBus.GetConnectionUnixProcessID",
            name,
        )
        match = re.search(r"uint32\s+(\d+)", result)
        if match is not None and int(match.group(1)) in pids:
            matches.append(name)
    if not matches:
        raise LinuxSandboxError("no AT-SPI bus name belongs to the enrolled WeChat process")
    return tuple(sorted(matches))


@dataclass(slots=True)
class SandboxedAtspiWorker:
    worker: WorkerProcess
    proxy: WorkerProcess
    runtime_directory: Path
    supervisor: WorkerSupervisor

    async def close_proxy(self) -> None:
        await self.supervisor.stop(self.proxy.name, grace_period_seconds=1)
        try:
            self.runtime_directory.rmdir()
        except OSError:
            pass


async def spawn_sandboxed_atspi_worker(
    supervisor: WorkerSupervisor,
    *,
    worker_python: Path,
    expected_pids: tuple[int, ...],
) -> SandboxedAtspiWorker:
    if not sys.platform.startswith("linux"):
        raise LinuxSandboxError("AT-SPI sandbox is Linux-only")
    required = {
        name: shutil.which(name)
        for name in ("bwrap", "gdbus", "systemd-run", "xdg-dbus-proxy")
    }
    if any(value is None for value in required.values()):
        raise LinuxSandboxError("required AT-SPI sandbox command is unavailable")
    gdbus = str(required["gdbus"])
    address = await _atspi_bus_address(gdbus)
    bus_names = await _wechat_bus_names(gdbus, address, frozenset(expected_pids))
    runtime_root = Path(os.environ.get("XDG_RUNTIME_DIR", ""))
    if not runtime_root.is_absolute() or not runtime_root.is_dir():
        raise LinuxSandboxError("XDG_RUNTIME_DIR is unavailable")
    runtime_directory = runtime_root / f"lemonbot-atspi-{uuid4().hex}"
    runtime_directory.mkdir(mode=0o700)
    proxy_socket = runtime_directory / "bus"
    proxy_name = f"atspi-proxy-{uuid4().hex}"
    proxy_unit = f"lemonbot-{proxy_name}.service"
    proxy_arguments = [
        "--user",
        "--pipe",
        "--wait",
        "--collect",
        "--quiet",
        f"--unit={proxy_unit}",
        "--property=NoNewPrivileges=yes",
        "--property=PrivateTmp=yes",
        "--property=ProtectSystem=strict",
        "--property=ProtectHome=yes",
        "--property=RestrictAddressFamilies=AF_UNIX",
        "--property=IPAddressDeny=any",
        "--property=MemoryMax=64M",
        "--property=TasksMax=8",
        "--property=BindsTo=lemonbot.service",
        "--property=After=lemonbot.service",
        "--",
        str(required["xdg-dbus-proxy"]),
        address,
        str(proxy_socket),
        "--filter",
        "--talk=org.a11y.atspi.Registry",
    ]
    proxy_arguments.extend(f"--talk={name}" for name in bus_names)
    proxy = await supervisor.spawn(
        proxy_name,
        Path(str(required["systemd-run"])),
        *proxy_arguments,
        cwd=runtime_directory,
        stream_limit_bytes=64 * 1024,
    )
    try:
        for _ in range(50):
            if proxy.process.returncode is not None:
                raise LinuxSandboxError("AT-SPI D-Bus proxy exited during startup")
            if proxy_socket.exists():
                break
            await asyncio.sleep(0.1)
        else:
            raise LinuxSandboxError("AT-SPI D-Bus proxy socket was not created")

        venv_root = worker_python.parent.parent
        if not venv_root.is_dir() or worker_python.resolve() != Path("/usr/bin/python3").resolve():
            raise LinuxSandboxError("AT-SPI worker must use the enrolled system-Python venv")
        readonly_mounts: list[str] = []
        for library in (Path("/lib"), Path("/lib64")):
            if library.exists():
                readonly_mounts.extend(("--ro-bind", str(library), str(library)))
        bwrap_arguments = [
            "--die-with-parent",
            "--new-session",
            "--unshare-net",
            "--unshare-pid",
            "--unshare-ipc",
            "--unshare-uts",
            "--clearenv",
            *readonly_mounts,
            "--ro-bind",
            "/usr",
            "/usr",
            "--ro-bind",
            str(venv_root),
            str(venv_root),
            "--proc",
            "/proc",
            "--dev",
            "/dev",
            "--tmpfs",
            "/tmp",  # noqa: S108 - private tmpfs inside the sandbox
            "--bind",
            str(runtime_directory),
            str(runtime_directory),
            "--setenv",
            "AT_SPI_BUS_ADDRESS",
            f"unix:path={proxy_socket}",
            "--setenv",
            "PYTHONNOUSERSITE",
            "1",
            "--chdir",
            "/tmp",  # noqa: S108 - private tmpfs inside the sandbox
            str(worker_python),
            "-I",
            "-m",
            "lemonbot.connectors.atspi_worker",
        ]
        worker_name = f"atspi-worker-{uuid4().hex}"
        worker = await supervisor.spawn(
            worker_name,
            Path(str(required["systemd-run"])),
            "--user",
            "--pipe",
            "--wait",
            "--collect",
            "--quiet",
            f"--unit=lemonbot-{worker_name}.service",
            "--property=NoNewPrivileges=yes",
            "--property=PrivateTmp=yes",
            "--property=ProtectSystem=strict",
            "--property=ProtectHome=tmpfs",
            f"--property=BindReadOnlyPaths={venv_root}",
            "--property=RestrictAddressFamilies=AF_UNIX",
            "--property=IPAddressDeny=any",
            "--property=MemoryMax=256M",
            "--property=TasksMax=8",
            "--property=KillMode=control-group",
            "--property=BindsTo=lemonbot.service",
            "--property=After=lemonbot.service",
            "--",
            str(required["bwrap"]),
            *bwrap_arguments,
            cwd=runtime_directory,
            stream_limit_bytes=1024 * 1024,
        )
        return SandboxedAtspiWorker(worker, proxy, runtime_directory, supervisor)
    except BaseException:
        await supervisor.stop(proxy_name, grace_period_seconds=1)
        try:
            runtime_directory.rmdir()
        except OSError:
            pass
        raise

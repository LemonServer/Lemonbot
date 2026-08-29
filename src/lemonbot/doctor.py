from __future__ import annotations

import hashlib
import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from lemonbot.config import AppSettings, RuntimePaths
from lemonbot.connectors.wechat_atspi import AtspiEnrollment
from lemonbot.security.secrets import (
    NamespacedSecretStore,
    SecretStoreError,
    platform_secret_store,
)
from lemonbot.tools.attachments import DEFAULT_MINIMUM_FREE_BYTES


@dataclass(frozen=True, slots=True)
class Check:
    name: str
    ok: bool
    detail: str
    required: bool = True


def run_checks(settings: AppSettings, paths: RuntimePaths) -> list[Check]:
    checks = [
        Check(
            "python",
            (3, 12) <= sys.version_info[:2] < (3, 13),
            platform.python_version(),
        ),
        Check(
            "platform",
            sys.platform.startswith("linux")
            and platform.machine().casefold() in {"x86_64", "amd64"},
            f"{platform.system()} {platform.release()} ({platform.machine()})",
        ),
        Check(
            "loopback-admin",
            settings.admin.host in {"127.0.0.1", "::1"},
            f"{settings.admin.host}:{settings.admin.port}",
        ),
        Check(
            "profile-channel-isolation",
            settings.runtime.connector != "wechat_atspi" or settings.profile == "lab",
            f"profile={settings.profile}, connector={settings.runtime.connector}",
        ),
        _data_disk_check(paths.root),
        _sqlite_check(paths.database),
    ]
    model_host = (settings.models.base_url.host or "").casefold()
    local_unmetered = (
        settings.models.provider == "openai_compatible"
        and model_host in {"127.0.0.1", "localhost", "::1"}
        and not settings.models.api_key_secret_name
    )
    if settings.models.provider not in {"disabled", "fake"} and not local_unmetered:
        checks.append(
            Check(
                "model-budget",
                settings.models.budget.enabled,
                (
                    "enabled with explicit prices"
                    if settings.models.budget.enabled
                    else "disabled (cloud calls fail closed)"
                ),
            )
        )
        if settings.models.api_key_secret_name:
            checks.append(_credential_check(settings.profile, settings.models.api_key_secret_name))
    if settings.vision.enabled:
        checks.append(_credential_check(settings.profile, "zhipu_api_key"))
    if settings.browser.enabled:
        checks.append(_playwright_check())
    if settings.runtime.connector == "wechat_atspi":
        atspi = settings.wechat_atspi
        checks.extend(
            [
                Check(
                    "wayland-session",
                    os.environ.get("XDG_SESSION_TYPE", "").casefold() == "wayland",
                    os.environ.get("XDG_SESSION_TYPE", "missing"),
                ),
                Check(
                    "wechat-executable",
                    Path(atspi.expected_executable_path).is_file()
                    and not Path(atspi.expected_executable_path).is_symlink(),
                    atspi.expected_executable_path,
                ),
                Check(
                    "atspi-worker-python",
                    _system_python_venv(Path(atspi.worker_python_path)),
                    (
                        "system-Python venv available"
                        if _system_python_venv(Path(atspi.worker_python_path))
                        else "missing or not based on /usr/bin/python3"
                    ),
                ),
                _linux_identity_check(atspi.expected_linux_uid, atspi.expected_linux_user),
                _executable_integrity_check(
                    Path(atspi.expected_executable_path), atspi.expected_executable_sha256
                ),
                _wechat_package_check(atspi.enrolled_client_version),
                _atspi_enrollment_check(settings),
                _command_check("systemd-run"),
                _command_check("bwrap"),
                _command_check("xdg-dbus-proxy"),
                _system_atspi_check(),
            ]
        )
    return checks


def _private_file(path: Path) -> bool:
    try:
        return (
            path.is_absolute()
            and path.is_file()
            and not path.is_symlink()
            and not path.stat().st_mode & 0o077
        )
    except OSError:
        return False


def _sha256_file(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as stream:
        for chunk in iter(lambda: stream.read(1024 * 1024), b""):
            digest.update(chunk)
    return digest.hexdigest()


def _system_python_venv(path: Path) -> bool:
    try:
        return path.is_file() and path.resolve(strict=True) == Path("/usr/bin/python3").resolve(
            strict=True
        )
    except OSError:
        return False


def _linux_identity_check(expected_uid: int, expected_user: str) -> Check:
    if not sys.platform.startswith("linux"):
        return Check("linux-identity", False, "not running on Linux")
    try:
        import pwd

        uid = os.getuid()
        user = pwd.getpwuid(uid).pw_name
    except (AttributeError, KeyError, OSError):
        return Check("linux-identity", False, "unavailable")
    return Check(
        "linux-identity",
        uid == expected_uid and user == expected_user,
        "matches enrollment" if uid == expected_uid and user == expected_user else "mismatch",
    )


def _executable_integrity_check(path: Path, expected_sha256: str) -> Check:
    try:
        valid = path.is_file() and not path.is_symlink() and _sha256_file(path) == expected_sha256
    except OSError:
        valid = False
    return Check(
        "wechat-executable-integrity",
        valid,
        "matches enrollment" if valid else "missing, linked or hash mismatch",
    )


def _wechat_package_check(expected_version: str) -> Check:
    try:
        result = subprocess.run(
            ["/usr/bin/dpkg-query", "-W", "-f=${Version}", "wechat"],
            check=False,
            capture_output=True,
            text=True,
            timeout=10,
        )
        version = result.stdout.strip().split("-", 1)[0]
        valid = result.returncode == 0 and version == expected_version
    except (OSError, subprocess.SubprocessError):
        valid = False
    return Check(
        "wechat-package-version",
        valid,
        "matches enrollment" if valid else "missing or version mismatch",
    )


def _atspi_enrollment_check(settings: AppSettings) -> Check:
    atspi = settings.wechat_atspi
    path = Path(atspi.enrollment_bundle_path)
    if not _private_file(path):
        return Check("atspi-enrollment", False, "missing or unsafe")
    try:
        enrollment = AtspiEnrollment.load(path, atspi.enrollment_bundle_sha256)
    except (OSError, ValueError):
        return Check("atspi-enrollment", False, "invalid or hash mismatch")
    refs = {target.target_ref for target in enrollment.targets}
    valid = (
        enrollment.account_fingerprint == atspi.account_fingerprint
        and enrollment.ui_signature == atspi.ui_signature
        and set(atspi.allow_target_refs) <= refs
    )
    return Check(
        "atspi-enrollment",
        valid,
        "identity and allowlist verified" if valid else "identity or allowlist mismatch",
    )


def _command_check(name: str) -> Check:
    available = shutil.which(name) is not None
    return Check(f"command:{name}", available, "available" if available else "missing")


def _system_atspi_check() -> Check:
    try:
        result = subprocess.run(
            ["/usr/bin/python3", "-I", "-c", "import gi; gi.require_version('Atspi','2.0')"],
            check=False,
            capture_output=True,
            timeout=10,
        )
    except (OSError, subprocess.SubprocessError):
        return Check("python3-gi-atspi", False, "unavailable")
    return Check(
        "python3-gi-atspi",
        result.returncode == 0,
        "available" if result.returncode == 0 else "missing",
    )


def _data_disk_check(
    data_root: Path,
    minimum_free_bytes: int = DEFAULT_MINIMUM_FREE_BYTES,
) -> Check:
    candidate = data_root.expanduser()
    while not candidate.exists() and candidate != candidate.parent:
        candidate = candidate.parent
    try:
        usage = shutil.disk_usage(candidate)
    except OSError as exc:
        return Check("data-disk-free", False, f"unavailable: {type(exc).__name__}", required=False)
    gib = 1024**3
    detail = (
        f"{usage.free / gib:.2f} GiB free; attachment reserve={minimum_free_bytes / gib:.2f} GiB"
    )
    return Check(
        "data-disk-free",
        usage.free >= minimum_free_bytes,
        detail,
        required=False,
    )


def _sqlite_check(database: Path) -> Check:
    del database  # the capability probe is intentionally non-mutating
    try:
        with sqlite3.connect(":memory:") as connection:
            connection.execute("CREATE VIRTUAL TABLE probe USING fts5(value, tokenize='trigram')")
            connection.execute("INSERT INTO probe(value) VALUES ('上下文压缩测试')")
            hit = connection.execute(
                "SELECT count(*) FROM probe WHERE probe MATCH '上下文'"
            ).fetchone()
        return Check("sqlite-wal-fts5", hit == (1,), sqlite3.sqlite_version)
    except sqlite3.Error as exc:
        return Check("sqlite-wal-fts5", False, str(exc))


def _playwright_check() -> Check:
    try:
        result = subprocess.run(
            [sys.executable, "-m", "playwright", "install", "--list"],
            check=False,
            capture_output=True,
            text=True,
            timeout=15,
        )
        installed = result.returncode == 0 and (
            "chromium-" in result.stdout.casefold()
            or "chromium_headless_shell-" in result.stdout.casefold()
        )
    except (OSError, subprocess.SubprocessError) as exc:
        return Check(
            "playwright-chromium",
            False,
            f"unavailable: {type(exc).__name__}",
        )
    return Check(
        "playwright-chromium",
        installed,
        "installed" if installed else "missing; run: uv run playwright install chromium",
    )


def _credential_check(profile: str, name: str) -> Check:
    try:
        store = NamespacedSecretStore(platform_secret_store(), profile)
        configured = store.get(name) is not None
        return Check(f"credential:{name}", configured, "configured" if configured else "missing")
    except SecretStoreError as exc:
        return Check(f"credential:{name}", False, str(exc))

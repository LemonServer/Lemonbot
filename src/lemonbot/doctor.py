from __future__ import annotations

import os
import platform
import shutil
import sqlite3
import subprocess
import sys
from dataclasses import dataclass
from pathlib import Path

from lemonbot.config import AppSettings, RuntimePaths
from lemonbot.security.secrets import (
    NamespacedSecretStore,
    SecretStoreError,
    WindowsCredentialStore,
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
            "windows",
            os.name == "nt" and platform.release() in {"10", "11"},
            f"{platform.system()} {platform.release()} ({platform.machine()})",
        ),
        Check(
            "loopback-admin",
            settings.admin.host in {"127.0.0.1", "::1"},
            f"{settings.admin.host}:{settings.admin.port}",
        ),
        Check(
            "profile-channel-isolation",
            not (
                (settings.runtime.connector == "wecom" and settings.profile != "prod")
                or (settings.runtime.connector == "wechat_uia" and settings.profile != "lab")
            ),
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
    if settings.models.provider != "fake" and not local_unmetered:
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
    if settings.runtime.connector == "wecom":
        checks.append(_credential_check(settings.profile, "wecom_bot_secret"))
    if settings.runtime.connector == "wechat_uia":
        checks.append(
            Check(
                "uia-stage",
                settings.wechat_uia.stage == "observe",
                f"initial stage={settings.wechat_uia.stage}",
                required=False,
            )
        )
    return checks


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
        store = NamespacedSecretStore(WindowsCredentialStore(), profile)
        configured = store.get(name) is not None
        return Check(f"credential:{name}", configured, "configured" if configured else "missing")
    except SecretStoreError as exc:
        return Check(f"credential:{name}", False, str(exc))

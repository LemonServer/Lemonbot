from __future__ import annotations

from pathlib import Path


def test_wechat_user_unit_uses_only_user_manager_compatible_sandboxing() -> None:
    unit = Path("deploy/systemd/lemonbot-wechat-accessible.service").read_text(
        encoding="utf-8"
    )

    for required in (
        "NoNewPrivileges=yes",
        "LockPersonality=yes",
        "PrivateTmp=yes",
        "RestrictRealtime=yes",
        "RestrictSUIDSGID=yes",
        "MemoryMax=2G",
        "TasksMax=256",
    ):
        assert required in unit
    # These settings need privileged mount/capability operations or an
    # unprivileged user namespace. In this VM's user manager they fail before
    # /usr/bin/wechat is executed with status 218/CAPABILITIES. The non-root
    # service account cannot load modules, read kernel logs, or alter system
    # files without these unsupported directives.
    for incompatible in (
        "ProtectSystem=",
        "ProtectControlGroups=",
        "ProtectKernelLogs=",
        "ProtectKernelModules=",
        "ProtectKernelTunables=",
    ):
        assert incompatible not in unit

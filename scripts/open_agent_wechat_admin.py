"""Open the private local management URL without printing its bootstrap token."""

from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from urllib.parse import urlsplit

from lemonbot.config import RuntimePaths
from lemonbot.config.settings import load_settings
from lemonbot.connectors.wechat_agent_state import private_read


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--config", required=True, type=Path)
    settings = load_settings(parser.parse_args().config)
    if settings.runtime.connector != "wechat_agent":
        raise SystemExit("WechatAgentConfigurationRequired")
    paths = RuntimePaths.from_settings(settings)
    url = private_read(paths.lock_file.parent / "lab.admin-url").decode()
    parts = urlsplit(url)
    if (
        parts.scheme != "http"
        or parts.hostname != settings.admin.host
        or parts.port != settings.admin.port
        or parts.path != "/login"
        or parts.query
        or parts.username
        or not parts.fragment
    ):
        raise SystemExit("InvalidLocalAdminURL")
    # The only argument is a verified loopback URL; no shell is invoked.
    subprocess.Popen(  # noqa: S603
        ["/usr/bin/xdg-open", url],
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
    )
    print("已请求打开本机管理台；链接凭据未输出。")


if __name__ == "__main__":
    main()

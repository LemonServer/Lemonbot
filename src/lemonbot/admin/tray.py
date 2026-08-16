from __future__ import annotations

import threading
import webbrowser
from collections.abc import Callable

from lemonbot.admin.auth import LocalTokenManager


def start_tray(
    tokens: LocalTokenManager,
    *,
    host: str,
    port: int,
    emergency_stop: Callable[[], None],
    set_pause: Callable[[str | None, bool], None],
) -> threading.Thread | None:
    try:
        import pystray  # type: ignore[import-untyped]
        from PIL import Image, ImageDraw
    except ImportError:
        return None

    image = Image.new("RGB", (64, 64), "#151515")
    draw = ImageDraw.Draw(image)
    draw.ellipse((10, 10, 54, 54), fill="#ffd84d")
    draw.ellipse((25, 23, 31, 29), fill="#151515")
    draw.ellipse((38, 23, 44, 29), fill="#151515")

    def open_admin(_icon=None, _item=None) -> None:  # type: ignore[no-untyped-def]
        token = tokens.issue_bootstrap()
        webbrowser.open(f"http://{host}:{port}/login#{token}")

    def stop(_icon=None, _item=None) -> None:  # type: ignore[no-untyped-def]
        emergency_stop()

    def pause_global(_icon=None, _item=None) -> None:  # type: ignore[no-untyped-def]
        set_pause(None, True)

    def resume_global(_icon=None, _item=None) -> None:  # type: ignore[no-untyped-def]
        set_pause(None, False)

    def pause_wecom(_icon=None, _item=None) -> None:  # type: ignore[no-untyped-def]
        set_pause("wecom", True)

    def resume_wecom(_icon=None, _item=None) -> None:  # type: ignore[no-untyped-def]
        set_pause("wecom", False)

    def pause_wechat(_icon=None, _item=None) -> None:  # type: ignore[no-untyped-def]
        set_pause("wechat_uia", True)

    def resume_wechat(_icon=None, _item=None) -> None:  # type: ignore[no-untyped-def]
        set_pause("wechat_uia", False)

    icon = pystray.Icon(
        "Lemonbot",
        image,
        "Lemonbot",
        menu=pystray.Menu(
            pystray.MenuItem("打开本地管理台", open_admin),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("暂停全部出站", pause_global),
            pystray.MenuItem("恢复全部出站", resume_global),
            pystray.MenuItem("暂停企业微信", pause_wecom),
            pystray.MenuItem("恢复企业微信", resume_wecom),
            pystray.MenuItem("暂停个人微信实验", pause_wechat),
            pystray.MenuItem("恢复个人微信实验", resume_wechat),
            pystray.Menu.SEPARATOR,
            pystray.MenuItem("紧急停止", stop),
        ),
    )
    thread = threading.Thread(target=icon.run, name="lemonbot-tray", daemon=True)
    thread.start()
    return thread

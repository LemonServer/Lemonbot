"""Sanitised Enterprise WeChat callback fixtures.

Values are synthetic.  In particular, response URLs and AES keys are not real
credentials and must still never appear in mapped domain metadata.
"""

from __future__ import annotations

from copy import deepcopy
from typing import Any


def direct_text_frame(*, msgid: str = "msg-direct-001") -> dict[str, Any]:
    return {
        "cmd": "aibot_msg_callback",
        "headers": {"req_id": "callback-direct-001"},
        "body": {
            "msgid": msgid,
            "aibotid": "bot-fixture",
            "chattype": "single",
            "from": {"userid": "user-alice"},
            "msgtype": "text",
            "create_time": 1_700_000_000,
            "response_url": "https://example.invalid/response?token=fixture-secret",
            "text": {"content": "你好, Lemonbot"},
        },
    }


def group_text_frame(*, msgid: str = "msg-group-001") -> dict[str, Any]:
    frame = direct_text_frame(msgid=msgid)
    frame["headers"]["req_id"] = "callback-group-001"
    frame["body"].update(
        {
            "chattype": "group",
            "chatid": "group-stable-42",
            "from": {"userid": "user-bob"},
        }
    )
    return frame


def image_frame(*, msgid: str = "msg-image-001") -> dict[str, Any]:
    frame = direct_text_frame(msgid=msgid)
    frame["headers"]["req_id"] = "callback-image-001"
    frame["body"].update(
        {
            "msgtype": "image",
            "image": {
                "url": "https://example.invalid/encrypted/image",
                "aeskey": "fixture-aes-key-must-not-leak",
            },
        }
    )
    frame["body"].pop("text", None)
    return frame


def enter_chat_frame() -> dict[str, Any]:
    return {
        "cmd": "aibot_event_callback",
        "headers": {"req_id": "callback-enter-001"},
        "body": {
            "chattype": "single",
            "from": {"userid": "user-alice"},
            "msgtype": "event",
            "event": {"eventtype": "enter_chat", "userid": "user-alice"},
        },
    }


def duplicate(frame: dict[str, Any]) -> dict[str, Any]:
    return deepcopy(frame)

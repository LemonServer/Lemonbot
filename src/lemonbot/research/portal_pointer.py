"""Explicitly consented, single-window pointer research. Never a connector."""

from __future__ import annotations

import importlib.util
import secrets
import sys
from pathlib import Path
from typing import Any

# The worker runs with system Python -I; keep runtime/model imports out of it.
_source = Path(__file__).with_name("portal_capture.py")
_spec = importlib.util.spec_from_file_location("_lemonbot_portal_capture", _source)
if _spec is None or _spec.loader is None:
    raise RuntimeError("Portal runtime unavailable")
portal = importlib.util.module_from_spec(_spec)
sys.modules[_spec.name] = portal
_spec.loader.exec_module(portal)
REMOTE_IFACE = "org.freedesktop.portal.RemoteDesktop"


def validate_pointer_stream(results: dict[str, Any]) -> tuple[int, tuple[int, int]]:
    if type(results.get("devices")) is not int or results["devices"] != 2:
        raise ValueError("pointer-only permission required")
    if results.get("clipboard_enabled", False) is not False:
        raise ValueError("clipboard permission not allowed")
    node_id = portal._single_stream(results)
    properties = portal._unpack(results["streams"][0][1])
    if not isinstance(properties, dict) or properties.get("source_type") != 2:
        raise ValueError("single window required")
    size = properties.get("logical_size", properties.get("size"))
    if (
        not isinstance(size, (list, tuple)) or len(size) != 2
        or any(type(value) is not int or not 1 <= value <= 16_384 for value in size)
    ):
        raise ValueError("logical window size unavailable")
    return int(node_id), (size[0], size[1])


class PointerSession:
    def __init__(self, *, timeout_seconds: int = 60) -> None:
        self.client = portal._PortalClient(timeout_seconds=timeout_seconds)
        self.handle: str | None = None
        self.node_id = 0
        self.logical_size = (0, 0)
        self.attempted = False
        self.stage = "created"
        self.response_facts: dict[str, object] = {}

    def open(self) -> None:
        client = self.client
        variant = client._glib.Variant
        token = f"lb_{secrets.token_hex(12)}"
        self.stage = "create_session"
        created = client._request("CreateSession", variant("(a{sv})", ({
            "handle_token": variant("s", token),
            "session_handle_token": variant("s", f"lb_{secrets.token_hex(12)}"),
        },)), token, interface=REMOTE_IFACE)
        handle = created.get("session_handle")
        if not isinstance(handle, str) or not handle.startswith(
            "/org/freedesktop/portal/desktop/session/"
        ):
            raise ValueError("invalid session")
        self.handle = handle
        try:
            token = f"lb_{secrets.token_hex(12)}"
            self.stage = "select_devices"
            client._request("SelectDevices", variant("(oa{sv})", (handle, {
                "handle_token": variant("s", token), "types": variant("u", 2),
                "persist_mode": variant("u", 0),
            })), token, interface=REMOTE_IFACE)
            token = f"lb_{secrets.token_hex(12)}"
            self.stage = "select_sources"
            client._request("SelectSources", variant("(oa{sv})", (handle, {
                "handle_token": variant("s", token), "types": variant("u", 2),
                "multiple": variant("b", False), "cursor_mode": variant("u", 1),
            })), token)
            token = f"lb_{secrets.token_hex(12)}"
            self.stage = "start"
            started = client._request("Start", variant("(osa{sv})", (handle, "", {
                "handle_token": variant("s", token),
            })), token, interface=REMOTE_IFACE)
            self.stage = "validate_response"
            streams = started.get("streams", [])
            self.response_facts = {
                "devices": started.get("devices") if type(started.get("devices")) is int else None,
                "stream_count": len(streams) if isinstance(streams, (list, tuple)) else None,
            }
            self.node_id, self.logical_size = validate_pointer_stream(started)
            self.stage = "ready"
        except BaseException:
            self.close()
            raise

    def _notify(self, method: str, signature: str, parameters: tuple[object, ...]) -> None:
        if self.handle is None:
            raise ValueError("session closed")
        client = self.client
        client._connection.call_sync(
            portal.PORTAL_BUS, portal.PORTAL_PATH, REMOTE_IFACE, method,
            client._glib.Variant(signature, parameters), None,
            client._gio.DBusCallFlags.NONE, client._timeout_ms, None,
        )

    def click_once(self, x: float, y: float) -> None:
        if self.attempted or not 0 <= x < self.logical_size[0] or not 0 <= y < self.logical_size[1]:
            raise ValueError("point invalid or already attempted")
        self.attempted = True
        self._notify("NotifyPointerMotionAbsolute", "(oa{sv}udd)",
                     (self.handle, {}, self.node_id, x, y))
        try:
            self._notify("NotifyPointerButton", "(oa{sv}iu)", (self.handle, {}, 0x110, 1))
        finally:
            self._notify("NotifyPointerButton", "(oa{sv}iu)", (self.handle, {}, 0x110, 0))

    def close(self) -> None:
        if self.handle is not None:
            handle, self.handle = self.handle, None
            self.client.close(handle)


if __name__ == "__main__":
    import json

    session = None
    try:
        session = PointerSession()
        session.open()
        print(json.dumps({"pointer_granted": True, "source_kind": "window",
                          "logical_size": session.logical_size, "actions_performed": 0}))
    except Exception:
        print(json.dumps({"error": "PortalPointerUnavailable", "actions_performed": 0,
                          "stage": session.stage if session else "initialization",
                          "response_facts": session.response_facts if session else {}}))
    finally:
        if session is not None:
            session.close()

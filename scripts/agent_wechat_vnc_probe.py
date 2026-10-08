#!/usr/bin/env python3
"""Read-only noVNC WebSocket/RFB greeting check; not proof of WeChat login."""
from __future__ import annotations

import base64
import hashlib
import json
import re
import secrets
import socket
import struct
import time
from urllib.parse import quote

import agent_wechat_repro as repro


class VNCProbeError(RuntimeError):
    """Only fixed codes, never response bodies or credential-bearing URLs."""


def probe(token: str) -> dict[str, object]:
    if not token or any(c.isspace() for c in token):
        raise VNCProbeError('InvalidToken')
    stream = socket.create_connection(('127.0.0.1', 6174), timeout=8)
    stream.settimeout(0.5)
    upgraded = False
    try:
        deadline = time.monotonic() + 8
        key = base64.b64encode(secrets.token_bytes(16)).decode()
        request = (
            f'GET /vnc/websockify?token={quote(token, safe="")} HTTP/1.1\r\n'
            'Host: 127.0.0.1:6174\r\nUpgrade: websocket\r\nConnection: Upgrade\r\n'
            f'Sec-WebSocket-Key: {key}\r\nSec-WebSocket-Version: 13\r\n'
            'Sec-WebSocket-Protocol: binary\r\n\r\n'
        )
        stream.sendall(request.encode('ascii'))
        header = bytearray()
        while b'\r\n\r\n' not in header:
            if len(header) >= 16384:
                raise VNCProbeError('InvalidUpgrade')
            header.extend(repro.websocket_read_exact(stream, 1, deadline))
        lines = header.decode('latin1').split('\r\n')
        status = lines[0].split(' ', 2)[1:2]
        if status in (['401'], ['403']):
            raise VNCProbeError('AuthenticationRejected')
        if status != ['101']:
            raise VNCProbeError('UpgradeRejected')
        fields = {}
        for line in lines[1:]:
            if ':' in line:
                name, value = line.split(':', 1)
                name = name.lower().strip()
                if name in fields:
                    raise VNCProbeError('InvalidUpgrade')
                fields[name] = value.strip()
        accept = base64.b64encode(
            hashlib.sha1((key + repro.WEBSOCKET_GUID).encode()).digest()  # noqa: S324
        ).decode()
        if (fields.get('sec-websocket-accept') != accept
                or fields.get('upgrade', '').lower() != 'websocket'
                or 'upgrade' not in {v.strip().lower()
                                     for v in fields.get('connection', '').split(',')}):
            raise VNCProbeError('InvalidUpgrade')
        if fields.get('sec-websocket-protocol') != 'binary':
            raise VNCProbeError('BinarySubprotocolMissing')
        upgraded = True
        greeting = bytearray()
        fragmented = False
        while len(greeting) < 12 or fragmented:
            first, second = repro.websocket_read_exact(stream, 2, deadline)
            opcode = first & 15
            size = second & 127
            if first & 0x70 or second & 0x80:
                raise VNCProbeError('InvalidFrame')
            if size == 126:
                size = struct.unpack('!H', repro.websocket_read_exact(stream, 2, deadline))[0]
            elif size == 127:
                size = struct.unpack('!Q', repro.websocket_read_exact(stream, 8, deadline))[0]
            if size > 4096:
                raise VNCProbeError('InvalidFrame')
            payload = repro.websocket_read_exact(stream, size, deadline)
            if opcode in {9, 10}:
                if not first & 128 or size > 125:
                    raise VNCProbeError('InvalidFrame')
                if opcode == 9:
                    repro.websocket_send_frame(stream, 10, payload)
                continue
            if opcode == 8:
                raise VNCProbeError('BridgeClosedBeforeGreeting')
            if opcode == 2 and not fragmented:
                fragmented = not bool(first & 128)
            elif opcode == 0 and fragmented:
                fragmented = not bool(first & 128)
            else:
                raise VNCProbeError('InvalidFrame')
            greeting.extend(payload)
            if len(greeting) > 4096:
                raise VNCProbeError('InvalidFrame')
        if not re.fullmatch(rb'RFB [0-9]{3}\.[0-9]{3}\n', bytes(greeting[:12])):
            raise VNCProbeError('InvalidRFBBanner')
        return {'vnc_transport_ready': True, 'binary_subprotocol': True,
                'rfb_version': bytes(greeting[4:11]).decode(), 'login_verified': False}
    finally:
        if upgraded:
            try:
                repro.websocket_send_frame(stream, 8)
            except OSError:
                pass
        stream.close()


if __name__ == '__main__':
    try:
        print(json.dumps(probe(repro.auth_token())))
    except VNCProbeError as error:
        print(json.dumps({'vnc_transport_ready': False, 'code': str(error)}))
        raise SystemExit(2) from None
    except (OSError, EOFError, ValueError, struct.error):
        print(json.dumps({'vnc_transport_ready': False, 'code': 'VNCTransportUnavailable'}))
        raise SystemExit(2) from None

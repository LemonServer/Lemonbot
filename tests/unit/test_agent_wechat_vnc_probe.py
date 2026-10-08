from __future__ import annotations

import base64
import hashlib
import importlib
import socket
import threading
from pathlib import Path
from types import ModuleType

import pytest


@pytest.fixture
def vnc(monkeypatch: pytest.MonkeyPatch) -> ModuleType:
    monkeypatch.syspath_prepend(str(Path(__file__).resolve().parents[2] / 'scripts'))
    return importlib.import_module('agent_wechat_vnc_probe')


@pytest.mark.parametrize('scenario,expected', [
    ('fragmented_ping', None),
    ('missing_binary', 'BinarySubprotocolMissing'),
    ('bad_accept', 'InvalidUpgrade'),
    ('unauthorized', 'AuthenticationRejected'),
    ('closed_bridge', 'BridgeClosedBeforeGreeting'),
    ('private_banner', 'InvalidRFBBanner'),
    ('unfinished_banner', 'BridgeClosedBeforeGreeting'),
])
def test_real_socket_bridge_checks_subprotocol_and_rfb_without_exposing_private_data(
    vnc: ModuleType, monkeypatch: pytest.MonkeyPatch, scenario: str, expected: str | None,
) -> None:
    listener = socket.socket()
    listener.bind(('127.0.0.1', 0))
    listener.listen(1)
    address = listener.getsockname()
    captured = []
    errors = []

    def server() -> None:
        try:
            connection, _ = listener.accept()
            with connection:
                connection.settimeout(3)
                request = bytearray()
                while b'\r\n\r\n' not in request:
                    data = connection.recv(1)
                    if not data:
                        raise EOFError
                    request.extend(data)
                captured.append(bytes(request))
                lines = request.decode().split('\r\n')
                headers = dict(line.split(': ', 1) for line in lines if ': ' in line)
                accept = base64.b64encode(hashlib.sha1(  # noqa: S324 - RFC 6455
                    (headers['Sec-WebSocket-Key'] + vnc.repro.WEBSOCKET_GUID).encode()
                ).digest()).decode()
                if scenario == 'unauthorized':
                    connection.sendall(b'HTTP/1.1 401 Unauthorized\r\n\r\n')
                    return
                if scenario == 'bad_accept':
                    accept = 'private-invalid-accept'
                response = (
                    'HTTP/1.1 101 Switching Protocols\r\n'
                    'Upgrade: websocket\r\nConnection: Upgrade\r\n'
                    f'Sec-WebSocket-Accept: {accept}\r\n'
                )
                if scenario != 'missing_binary':
                    response += 'Sec-WebSocket-Protocol: binary\r\n'
                connection.sendall((response + '\r\n').encode())
                if scenario in {'bad_accept', 'missing_binary'}:
                    connection.recv(32)
                    return
                if scenario == 'closed_bridge':
                    wire = b'\x88\x00'
                elif scenario == 'unfinished_banner':
                    wire = b'\x02\x0cRFB 003.008\n\x88\x00'
                elif scenario == 'private_banner':
                    wire = b'\x82\x0csecret-value'
                else:
                    wire = b'\x02\x03RFB\x89\x04ping\x80\x09 003.008\n'
                connection.sendall(wire)
                connection.recv(32)
        except Exception as error:
            errors.append(type(error).__name__)
        finally:
            listener.close()

    thread = threading.Thread(target=server, daemon=True)
    thread.start()
    connect = socket.create_connection
    monkeypatch.setattr(vnc.socket, 'create_connection',
                        lambda _address, timeout: connect(address, timeout=timeout))
    if expected:
        with pytest.raises(vnc.VNCProbeError, match=expected) as error:
            vnc.probe('private-token')
        assert 'private-' not in str(error.value) and 'secret-value' not in str(error.value)
    else:
        result = vnc.probe('private-token')
        assert result == {'vnc_transport_ready': True, 'binary_subprotocol': True,
                          'rfb_version': '003.008', 'login_verified': False}
    thread.join(timeout=4)
    assert not thread.is_alive() and not errors
    assert b'GET /vnc/websockify?token=private-token HTTP/1.1' in captured[0]
    assert b'Sec-WebSocket-Protocol: binary' in captured[0]
    assert b'Authorization:' not in captured[0]  # mirror the browser's query auth


@pytest.mark.parametrize('token', ['', 'private\ntoken'])
def test_invalid_token_never_opens_connection(
    vnc: ModuleType, monkeypatch: pytest.MonkeyPatch, token: str,
) -> None:
    def unexpected(*args: object, **kwargs: object) -> None:
        pytest.fail('connection opened for invalid credential')
    monkeypatch.setattr(vnc.socket, 'create_connection', unexpected)
    with pytest.raises(vnc.VNCProbeError, match='InvalidToken'):
        vnc.probe(token)

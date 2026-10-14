"""WebSocket transport: URL mapping, TLS options and a real socket round trip."""

import asyncio
import json
import socket
import ssl

import pytest
import websockets
from websockets.asyncio.server import ServerConnection

from playtest.protocol import ProtocolError
from playtest.transport import (
    TransportError,
    WebSocketTransport,
    build_ssl_context,
    is_loopback,
    websocket_url,
)


def free_port() -> int:
    """Return a currently unused local port."""
    with socket.socket() as probe:
        probe.bind(("127.0.0.1", 0))
        return int(probe.getsockname()[1])


def test_gateway_url_mapping() -> None:
    """http(s) base URLs map onto the gateway path with the right scheme."""
    assert websocket_url("https://127.0.0.1:4141", "abc") == "wss://127.0.0.1:4141/ws/abc"
    assert websocket_url("http://host:8080/", "abc") == "ws://host:8080/ws/abc"
    assert websocket_url("https://host/base", "abc") == "wss://host/base/ws/abc"


@pytest.mark.parametrize("bad", ["ftp://host:1", "wss://host:1", "https://"])
def test_bad_server_urls_are_rejected(bad: str) -> None:
    """Only http(s) URLs with a host are usable."""
    with pytest.raises(TransportError):
        websocket_url(bad, "abc")


def test_ssl_context_follows_the_verify_flag() -> None:
    """The default server certificate is self-signed, so verification is optional."""
    skip = build_ssl_context(False)
    assert skip.check_hostname is False
    assert skip.verify_mode == ssl.CERT_NONE

    strict = build_ssl_context(True)
    assert strict.check_hostname is True
    assert strict.verify_mode == ssl.CERT_REQUIRED


def test_broken_connections_raise_transport_errors() -> None:
    """Using a transport before opening it, or against a dead port, fails clearly."""
    transport = WebSocketTransport("ws://127.0.0.1:1/ws/x", open_timeout=2.0)

    async def run() -> None:
        with pytest.raises(TransportError):
            await transport.send({"event_type": "chat", "data": {}})
        with pytest.raises(TransportError):
            await transport.receive()
        closed = WebSocketTransport(f"ws://127.0.0.1:{free_port()}/ws/x", open_timeout=2.0)
        with pytest.raises(TransportError):
            await closed.open()
        await closed.close()

    asyncio.run(run())


def test_round_trip_over_a_real_socket() -> None:
    """Envelopes survive a real WebSocket hop, including non-ASCII text."""
    received: list[str] = []
    port = free_port()

    async def handler(socket: ServerConnection) -> None:
        async for message in socket:
            received.append(str(message))
            await socket.send(json.dumps({"type": "echo", "payload": {"text": message}}))

    async def run() -> None:
        async with websockets.serve(handler, "127.0.0.1", port):
            transport = WebSocketTransport(f"ws://127.0.0.1:{port}/ws/client")
            await transport.open()
            assert transport.connected is True
            await transport.send({"event_type": "action", "data": {"action": "开门"}})
            message = await transport.receive()
            assert message["type"] == "echo"
            assert "开门" in message["payload"]["text"]
            await transport.close()
            assert transport.connected is False

    asyncio.run(run())

    assert len(received) == 1
    assert json.loads(received[0])["event_type"] == "action"


def test_loopback_is_never_proxied(monkeypatch: pytest.MonkeyPatch) -> None:
    """A system SOCKS proxy must not hijack a self-hosted server connection.

    websockets follows the system proxy by default, which fails outright (and
    demands python-socks) even when the server is on this machine.
    """
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")
    monkeypatch.setenv("HTTP_PROXY", "socks5://127.0.0.1:1")
    monkeypatch.setenv("HTTPS_PROXY", "socks5://127.0.0.1:1")
    port = free_port()

    async def handler(socket: ServerConnection) -> None:
        await socket.send(json.dumps({"type": "pong", "payload": {}}))

    async def run() -> None:
        async with websockets.serve(handler, "127.0.0.1", port):
            transport = WebSocketTransport(f"ws://127.0.0.1:{port}/ws/client")
            assert transport.proxy_option() is None
            await transport.open()
            assert (await transport.receive())["type"] == "pong"
            await transport.close()

    asyncio.run(run())


def test_opting_into_the_system_proxy_actually_uses_it(monkeypatch: pytest.MonkeyPatch) -> None:
    """server.proxy=true hands the connection to the configured proxy."""
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")
    port = free_port()
    transport = WebSocketTransport(f"ws://127.0.0.1:{port}/ws/client", use_proxy=True)

    # Loopback is still direct, whatever the configuration says.
    assert transport.proxy_option() is None

    remote = WebSocketTransport("wss://example.invalid/ws/client", use_proxy=True)
    assert remote.proxy_option() is True
    assert WebSocketTransport("wss://example.invalid/ws/client").proxy_option() is None


def test_a_missing_socks_dependency_is_explained(monkeypatch: pytest.MonkeyPatch) -> None:
    """The python-socks ImportError becomes an actionable transport error."""
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")
    transport = WebSocketTransport("wss://example.invalid/ws/client", use_proxy=True)

    async def boom(*args: object, **kwargs: object) -> object:
        raise ImportError("connecting through a SOCKS proxy requires python-socks")

    monkeypatch.setattr("playtest.transport.websockets.connect", boom)

    async def run() -> None:
        with pytest.raises(TransportError) as excinfo:
            await transport.open()
        message = str(excinfo.value)
        assert "python-socks" in message
        assert "server.proxy" in message
        assert "example.invalid" in message

    asyncio.run(run())


@pytest.mark.parametrize(
    "host, expected",
    [
        ("127.0.0.1", True),
        ("127.1.2.3", True),
        ("localhost", True),
        ("::1", True),
        ("192.168.1.10", False),
        ("example.com", False),
    ],
)
def test_loopback_detection(host: str, expected: bool) -> None:
    """Only this machine's own addresses count as loopback."""
    assert is_loopback(host) is expected


def test_malformed_server_messages_are_rejected() -> None:
    """A non-JSON frame is a protocol error, not a silent empty event."""
    port = free_port()

    async def handler(socket: ServerConnection) -> None:
        await socket.send("this is not json")

    async def run() -> None:
        async with websockets.serve(handler, "127.0.0.1", port):
            transport = WebSocketTransport(f"ws://127.0.0.1:{port}/ws/client")
            await transport.open()
            with pytest.raises(ProtocolError):
                await transport.receive()
            await transport.close()

    asyncio.run(run())

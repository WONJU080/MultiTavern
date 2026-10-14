"""One WebSocket connection per playtest client.

The driver talks to the same ``/ws/{client_id}`` gateway a browser uses, so the
transport is deliberately thin: open, send an envelope, receive one raw message,
close. Everything above it works on decoded envelopes.
"""

from __future__ import annotations

import json
import ssl
from typing import Any, Protocol
from urllib.parse import urlsplit, urlunsplit

import websockets

from playtest.protocol import ProtocolError

DEFAULT_MAX_MESSAGE_BYTES = 8 * 1024 * 1024


class TransportError(RuntimeError):
    """Raised when a connection cannot be opened, used or read."""


class Transport(Protocol):
    """A duplex envelope channel for one client id."""

    async def open(self) -> None:
        """Establish the channel; a no-op for already-open transports."""
        ...

    async def send(self, envelope: dict[str, Any]) -> None:
        """Send one client envelope."""
        ...

    async def receive(self) -> dict[str, Any]:
        """Return the next raw server message."""
        ...

    async def close(self) -> None:
        """Close the channel, tolerating an already-closed socket."""
        ...


def is_loopback(host: str) -> bool:
    """Return whether a host names this machine."""
    if host in ("localhost", "::1", "[::1]"):
        return True
    return host.startswith("127.")


def is_local_address(host: str) -> bool:
    """Return whether a host is this machine or the local network.

    A proxy cannot route back to loopback or the LAN, so such endpoints are
    always contacted directly — even when proxying is enabled.
    """
    if is_loopback(host) or host.endswith(".local"):
        return True
    parts = host.split(".")
    if len(parts) == 4 and all(part.isdigit() for part in parts):
        first, second = int(parts[0]), int(parts[1])
        return (
            first == 10 or (first == 172 and 16 <= second <= 31) or (first == 192 and second == 168)
        )
    return False


def websocket_url(base_url: str, client_id: str) -> str:
    """Build the gateway URL for an http(s) server base URL."""
    parts = urlsplit(base_url)
    scheme = {"https": "wss", "http": "ws"}.get(parts.scheme)
    if scheme is None:
        raise TransportError(f"服务器地址必须是 http(s)：{base_url}")
    if not parts.netloc:
        raise TransportError(f"服务器地址缺少主机名：{base_url}")
    path = parts.path.rstrip("/") + f"/ws/{client_id}"
    return urlunsplit((scheme, parts.netloc, path, "", ""))


def build_ssl_context(verify_tls: bool) -> ssl.SSLContext:
    """Return a client TLS context, skipping verification when asked.

    An Anyworld server uses a self-signed certificate by default, so a fresh
    checkout cannot connect without this.
    """
    context = ssl.create_default_context()
    if not verify_tls:
        context.check_hostname = False
        context.verify_mode = ssl.CERT_NONE
    return context


class WebSocketTransport:
    """A ``websockets`` client connection to the Anyworld gateway."""

    def __init__(
        self,
        url: str,
        *,
        verify_tls: bool = False,
        open_timeout: float = 15.0,
        ping_interval: float | None = 20.0,
        ping_timeout: float | None = 20.0,
        max_size: int = DEFAULT_MAX_MESSAGE_BYTES,
        use_proxy: bool = False,
    ) -> None:
        """Store connection options; nothing is opened until ``open()``."""
        self.url = url
        self.verify_tls = verify_tls
        self.open_timeout = open_timeout
        self.ping_interval = ping_interval
        self.ping_timeout = ping_timeout
        self.max_size = max_size
        self.use_proxy = use_proxy
        self._socket: Any = None

    @classmethod
    def connect_to(
        cls,
        base_url: str,
        client_id: str,
        *,
        verify_tls: bool = False,
        open_timeout: float = 15.0,
        use_proxy: bool = False,
    ) -> "WebSocketTransport":
        """Build a transport for one client id on a server base URL."""
        return cls(
            websocket_url(base_url, client_id),
            verify_tls=verify_tls,
            open_timeout=open_timeout,
            use_proxy=use_proxy,
        )

    @property
    def connected(self) -> bool:
        """Return whether this transport currently holds a socket."""
        return self._socket is not None

    def _require_socket(self) -> Any:
        if self._socket is None:
            raise TransportError("连接尚未建立。")
        return self._socket

    def proxy_option(self) -> bool | None:
        """Return the ``websockets`` proxy setting for this connection.

        ``None`` disables proxying entirely, ``True`` follows the system and
        environment configuration. Loopback is always direct: a proxy cannot
        reach this machine's own server, and a SOCKS proxy without python-socks
        would fail the connection outright.
        """
        host = urlsplit(self.url).hostname or ""
        if is_loopback(host) or not self.use_proxy:
            return None
        return True

    async def open(self) -> None:
        """Open the socket, translating connection failures."""
        if self._socket is not None:
            return
        options: dict[str, Any] = {
            "open_timeout": self.open_timeout,
            "ping_interval": self.ping_interval,
            "ping_timeout": self.ping_timeout,
            "max_size": self.max_size,
            "proxy": self.proxy_option(),
        }
        if self.url.startswith("wss://"):
            options["ssl"] = build_ssl_context(self.verify_tls)
        try:
            self._socket = await websockets.connect(self.url, **options)
        except ImportError as exc:
            # websockets asks for python-socks only when a SOCKS proxy is configured.
            raise TransportError(
                f"无法连接 {self.url}：系统配置了 SOCKS 代理，但缺少 python-socks。"
                "请把配置里的 server.proxy 设为 false（默认即直连），或安装 python-socks。"
            ) from exc
        except (OSError, websockets.WebSocketException) as exc:
            raise TransportError(f"无法连接 {self.url}: {exc}") from exc

    async def send(self, envelope: dict[str, Any]) -> None:
        """Send one envelope as JSON text."""
        socket = self._require_socket()
        try:
            await socket.send(json.dumps(envelope, ensure_ascii=False))
        except (websockets.WebSocketException, OSError) as exc:
            raise TransportError(f"发送失败：{exc}") from exc

    async def receive(self) -> dict[str, Any]:
        """Receive one JSON message, rejecting malformed payloads."""
        socket = self._require_socket()
        try:
            raw = await socket.recv()
        except websockets.ConnectionClosed as exc:
            raise TransportError(f"连接已关闭：{exc}") from exc
        except (websockets.WebSocketException, OSError) as exc:
            raise TransportError(f"接收失败：{exc}") from exc
        if isinstance(raw, bytes):
            raw = raw.decode("utf-8", errors="replace")
        try:
            decoded = json.loads(raw)
        except json.JSONDecodeError as exc:
            raise ProtocolError(f"服务器消息不是合法 JSON：{exc}") from exc
        if not isinstance(decoded, dict):
            raise ProtocolError("服务器消息不是对象。")
        return decoded

    async def close(self) -> None:
        """Close the socket once, tolerating an already-broken connection."""
        socket, self._socket = self._socket, None
        if socket is None:
            return
        try:
            await socket.close()
        except Exception:  # noqa: BLE001 - closing must never mask the run outcome
            pass

"""Room registry, invite-code generation and room lifecycle sweeping."""

import asyncio
import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any, Callable
from urllib.parse import urlparse

import httpx

from api.connections import ConnectionManager
from core.config import settings
from core.schemas import ServerEvent
from core.secrets import SecretDecryptionError, decrypt_text, encrypt_text
from logic.engine import GameEngine, restore_engine
from logic.models import GameState
from logic.round_history import RoundHistoryStore

LOGGER = logging.getLogger(__name__)

# Unambiguous uppercase alphabet: no I, L, O, 0 or 1.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_GROUPS = (3, 3)

ROOMS_DIR = Path(".rooms")

_ROOM_LIMIT_MESSAGE = "服务器房间数量已达上限，请稍后再试。"
_BORROW_DISABLED_MESSAGE = "服主已关闭借用 API，请选用自己的api。"
_KEY_REQUIRED_MESSAGE = "建房需要填入自己的 API Key，或选择借用服主的 API。"
_KEY_TOO_LONG_MESSAGE = "API Key 过长，请检查后重试。"
_ENDPOINT_MESSAGE = "自定义 endpoint 必须是 http(s) 地址。"
_ENDPOINT_BLOCKED_MESSAGE = "该 endpoint 不在服务器允许的范围内。"
_KEY_INVALID_MESSAGE = "服务商拒绝了该 API Key（401/403），请检查后重试。"
_KEYRING_UNAVAILABLE_MESSAGE = "服务器密钥环不可用，暂时无法保存 API Key。"
_NUMERALS = ("零", "一", "两", "三", "四", "五", "六", "七", "八", "九", "十")

_PRE_GAME_STATES = frozenset(
    {
        GameState.AWAITING_HOST,
        GameState.SCENARIO_INJECTION,
        GameState.AWAITING_PLAYERS,
    }
)


def _borrow_limit_message(limit: int) -> str:
    """Return the exact rejection message for a full borrowed-API slot pool."""
    count = _NUMERALS[limit] if 0 <= limit <= 10 else str(limit)
    return f"已有{count}个房间正在使用，请选用自己的api"


def _validated_endpoint(value: object) -> str:
    """Validate a host-supplied endpoint URL and enforce the optional host allowlist."""
    if not isinstance(value, str):
        raise ValueError(_ENDPOINT_MESSAGE)
    endpoint = value.strip()
    if not endpoint or len(endpoint) > 2_048:
        raise ValueError(_ENDPOINT_MESSAGE)
    parsed = urlparse(endpoint)
    if parsed.scheme not in ("http", "https") or not parsed.hostname:
        raise ValueError(_ENDPOINT_MESSAGE)
    allowlist = settings.server.allowed_llm_hosts
    if allowlist is not None:
        allowed = {host.strip().lower() for host in allowlist if host and host.strip()}
        if parsed.hostname.lower() not in allowed:
            raise ValueError(_ENDPOINT_BLOCKED_MESSAGE)
    return endpoint


def _parse_api_key(value: object) -> str:
    """Validate a host-supplied API key without ever echoing it back."""
    if not isinstance(value, str) or not value.strip():
        raise ValueError(_KEY_REQUIRED_MESSAGE)
    api_key = value.strip()
    if len(api_key) > 512:
        raise ValueError(_KEY_TOO_LONG_MESSAGE)
    return api_key


def _parse_llm_request(data: dict[str, Any]) -> tuple[bool, dict[str, Any]]:
    """Split a create_room payload's llm section.

    Returns (uses_server_key, own_overrides). The default when no llm section is
    present is borrowing the server's API, which also covers older clients.
    """
    raw = data.get("llm")
    if raw is None:
        return True, {}
    if not isinstance(raw, dict):
        raise ValueError("'llm' 必须是对象。")
    mode = raw.get("mode", "borrow")
    if mode == "borrow":
        return True, {}
    if mode != "own":
        raise ValueError("'llm.mode' 必须是 'own' 或 'borrow'。")
    overrides: dict[str, Any] = {"api_key": _parse_api_key(raw.get("api_key"))}
    provider = raw.get("provider")
    if provider is not None:
        if provider not in ("compatible", "openai"):
            raise ValueError("'llm.provider' 必须是 'compatible' 或 'openai'。")
        overrides["provider"] = provider
    endpoint = raw.get("endpoint")
    if endpoint is not None:
        overrides["endpoint"] = _validated_endpoint(endpoint)
    model_name = raw.get("model_name")
    if model_name is not None:
        if not isinstance(model_name, str) or not model_name.strip():
            raise ValueError("'llm.model_name' 必须是非空字符串。")
        overrides["model_name"] = model_name.strip()[:200]
    return False, overrides


def _build_room_llm_config(overrides: dict[str, Any]) -> Any:
    """Merge host overrides onto the server's default LLM configuration."""
    return settings.llm.model_copy(update=overrides)


async def _probe_llm_key(config: Any) -> None:
    """Fail fast on credentials the provider rejects; tolerate network trouble."""
    if config.provider == "compatible":
        url = config.endpoint.rstrip("/") + "/models"
    else:
        url = "https://api.openai.com/v1/models"
    try:
        async with httpx.AsyncClient(timeout=5.0) as client:
            response = await client.get(url, headers={"Authorization": f"Bearer {config.api_key}"})
    except httpx.HTTPError:
        # Unreachable/offline providers are not proof of a bad key.
        return
    if response.status_code in (401, 403):
        raise ValueError(_KEY_INVALID_MESSAGE)


@dataclass
class Room:
    """One independent game session: engine, connections and activity stamps."""

    code: str
    engine: GameEngine
    connections: ConnectionManager
    created_at: float
    last_active: float
    llm_meta: dict[str, Any] | None = None


def normalize_invite_code(value: object) -> str:
    """Uppercase and strip separators from a client-supplied invite code."""
    if not isinstance(value, str):
        raise ValueError("'invite_code' 必须是字符串")
    compact = "".join(ch for ch in value.upper() if ch.isalnum())
    if not compact:
        raise ValueError("'invite_code' 必须是 1-12 个字符")
    if len(compact) > 12:
        raise ValueError("'invite_code' 最多 12 个字符")
    return compact


def format_invite_code(compact: str) -> str:
    """Format a compact code for display, such as ABC-123."""
    groups = []
    offset = 0
    for length in CODE_GROUPS:
        groups.append(compact[offset : offset + length])
        offset += length
    if offset < len(compact):
        groups.append(compact[offset:])
    return "-".join(groups)


class RoomRegistry:
    """Own every live room and remove empty or ended rooms."""

    def __init__(self, resolver_factory: Callable[[], Any]) -> None:
        """Initialize the registry with the resolver factory for new engines."""
        self.rooms: dict[str, Room] = {}
        self._resolver_factory = resolver_factory
        self._sweep_task: asyncio.Task | None = None
        self._closed = False
        self._pending_creations = 0

    def generate_code(self) -> str:
        """Return a unique, compact invite code from the unambiguous alphabet."""
        for _ in range(100):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(sum(CODE_GROUPS)))
            if code not in self.rooms:
                return code
        raise RuntimeError("Could not generate a unique invite code")

    def borrowed_rooms_in_use(self) -> int:
        """Count live rooms that consume the server owner's API key."""
        return sum(
            1
            for room in self.rooms.values()
            if room.llm_meta is None and room.engine.state is not GameState.ENDED
        )

    def _apply_room_config(self, engine: GameEngine, llm_config: Any) -> None:
        """Hand a room's own LLM configuration to its resolver when supported."""
        apply = getattr(engine.resolver, "apply_llm_config", None)
        if callable(apply):
            apply(llm_config)

    def _require_room_key(self, engine: GameEngine) -> None:
        """Block inference for a room whose stored key cannot be recovered."""
        require = getattr(engine.resolver, "require_key", None)
        if callable(require):
            require()

    async def create_room(self, client_id: str, data: dict[str, Any]) -> Room:
        """Create a fresh room with its own engine and per-room API credentials.

        Borrowing the server owner's API key is capped server-wide; using a
        host-supplied key is always allowed. Room creation has no password gate.
        """
        uses_server_key, overrides = _parse_llm_request(data)
        room_limit = settings.server.max_rooms
        if room_limit is not None and len(self.rooms) + self._pending_creations >= room_limit:
            raise ValueError(_ROOM_LIMIT_MESSAGE)
        llm_meta: dict[str, Any] | None = None
        llm_config: Any | None = None
        if uses_server_key:
            limit = settings.server.borrowed_room_limit
            if limit <= 0:
                raise ValueError(_BORROW_DISABLED_MESSAGE)
            if self.borrowed_rooms_in_use() >= limit:
                raise ValueError(_borrow_limit_message(limit))
        else:
            llm_config = _build_room_llm_config(overrides)
            self._pending_creations += 1
            try:
                await _probe_llm_key(llm_config)
            finally:
                self._pending_creations -= 1
            try:
                key_cipher = encrypt_text(str(overrides["api_key"]))
            except SecretDecryptionError as exc:
                raise ValueError(_KEYRING_UNAVAILABLE_MESSAGE) from exc
            llm_meta = {
                name: overrides[name]
                for name in ("provider", "endpoint", "model_name")
                if name in overrides
            }
            llm_meta["key_cipher"] = key_cipher
        code = self.generate_code()
        connections = ConnectionManager()
        engine = GameEngine(connections, self._resolver_factory())
        if llm_config is not None:
            self._apply_room_config(engine, llm_config)
        room = Room(
            code=code,
            engine=engine,
            connections=connections,
            created_at=time(),
            last_active=time(),
            llm_meta=llm_meta,
        )
        engine.room_code = format_invite_code(code)
        registry = self

        async def on_ended(reason: str) -> None:
            await registry.remove_room(code, reason, notify=True)

        engine.on_ended = on_ended
        self.rooms[code] = room
        LOGGER.info(
            "Room created code=%s credentials=%s",
            code,
            "server" if room.llm_meta is None else "host",
        )
        return room

    async def provide_room_key(self, room: Room, client_id: str, data: dict[str, Any]) -> None:
        """Store and apply a fresh host API key for a key-blocked room."""
        if room.llm_meta is None:
            raise ValueError("该房间已有可用的 API 配置。")
        engine = room.engine
        player = engine.players.get(client_id)
        if player is None or not (player.is_host or client_id == engine.host_client_id):
            raise ValueError("只有房主可以为该房间提供 API Key。")
        api_key = _parse_api_key(data.get("api_key"))
        overrides = {
            name: room.llm_meta[name]
            for name in ("provider", "endpoint", "model_name")
            if name in room.llm_meta
        }
        overrides["api_key"] = api_key
        llm_config = _build_room_llm_config(overrides)
        try:
            room.llm_meta["key_cipher"] = encrypt_text(api_key)
        except SecretDecryptionError as exc:
            raise ValueError(_KEYRING_UNAVAILABLE_MESSAGE) from exc
        self._apply_room_config(engine, llm_config)
        self.save_room(room)
        LOGGER.info("Room key restored code=%s", room.code)

    def find_room(self, invite_code: object) -> Room:
        """Return the room for a client-supplied invite code."""
        code = normalize_invite_code(invite_code)
        room = self.rooms.get(code)
        if room is None:
            raise ValueError("找不到该房间，请检查邀请码。")
        return room

    def touch(self, room: Room) -> None:
        """Refresh a room's activity stamp."""
        room.last_active = time()

    def discard(self, code: str) -> None:
        """Drop a never-joined room without socket work."""
        if self.rooms.pop(code, None) is not None:
            self._delete_room_file(code)
            LOGGER.info("Room discarded code=%s", code)

    def _room_path(self, code: str) -> Path:
        return ROOMS_DIR / f"{code}.json"

    def save_room(self, room: Room) -> None:
        """Persist a room's state to disk atomically, encrypting host credentials."""
        try:
            ROOMS_DIR.mkdir(parents=True, exist_ok=True)
            data = {"code": room.code, **room.engine.to_persistent_dict()}
            if room.llm_meta is not None:
                data["llm"] = dict(room.llm_meta)
            tmp = self._room_path(room.code).with_suffix(".tmp")
            tmp.write_text(json.dumps(data, ensure_ascii=False), encoding="utf-8")
            tmp.replace(self._room_path(room.code))
        except OSError as exc:
            LOGGER.warning("Could not save room %s: %s", room.code, exc)

    def save_all(self) -> None:
        """Persist every live room."""
        for room in self.rooms.values():
            self.save_room(room)

    def _delete_room_file(self, code: str) -> None:
        try:
            self._room_path(code).unlink(missing_ok=True)
            RoundHistoryStore.delete(code)
        except OSError:
            pass

    def restore_rooms(self) -> None:
        """Rebuild every persisted room from disk, restoring per-room credentials."""
        if not ROOMS_DIR.exists():
            return
        for path in ROOMS_DIR.glob("*.json"):
            try:
                data = json.loads(path.read_text(encoding="utf-8"))
                code = data["code"]
                if code in self.rooms:
                    continue
                connections = ConnectionManager()
                engine = restore_engine(data, connections, self._resolver_factory)
                engine.room_code = format_invite_code(code)
                llm_meta = self._restore_room_llm(engine, data.get("llm"), code)
                room = Room(
                    code=code,
                    engine=engine,
                    connections=connections,
                    created_at=time(),
                    last_active=time(),
                    llm_meta=llm_meta,
                )
                registry = self

                async def on_ended(reason: str) -> None:
                    await registry.remove_room(code, reason, notify=True)

                engine.on_ended = on_ended
                self.rooms[code] = room
                LOGGER.info(
                    "Room restored code=%s state=%s credentials=%s",
                    code,
                    engine.state.name,
                    "server" if llm_meta is None else "host",
                )
            except Exception as exc:  # noqa: BLE001 - a corrupt file must not block startup
                LOGGER.warning("Could not restore room %s: %s", path.name, exc)

    def _restore_room_llm(
        self, engine: GameEngine, raw: object, code: str
    ) -> dict[str, Any] | None:
        """Restore a room's stored credentials, or block it until a key arrives.

        A room whose stored ciphertext cannot be decrypted must never silently
        fall back to the server owner's credentials.
        """
        if not isinstance(raw, dict):
            # Rooms persisted before per-room keys existed borrow the server API.
            return None
        meta = {
            name: raw[name]
            for name in ("provider", "endpoint", "model_name", "key_cipher")
            if name in raw
        }
        key_cipher = meta.pop("key_cipher", None)
        if not isinstance(key_cipher, str):
            self._require_room_key(engine)
            return meta
        try:
            api_key = decrypt_text(key_cipher)
        except SecretDecryptionError:
            LOGGER.warning("Room %s requires a fresh API key: stored key is unreadable", code)
            self._require_room_key(engine)
            return {**meta, "key_cipher": key_cipher}
        llm_config = _build_room_llm_config({**meta, "api_key": api_key})
        self._apply_room_config(engine, llm_config)
        return {**meta, "key_cipher": key_cipher}

    async def remove_room(self, code: str, reason: str, *, notify: bool) -> None:
        """Close a room: notify members and close its sockets."""
        room = self.rooms.pop(code, None)
        if room is None:
            return
        self._delete_room_file(code)
        room.engine.on_ended = None
        if notify:
            await room.connections.broadcast_global(
                ServerEvent(type="room_closed", payload={"msg": reason})
            )
        await room.connections.close()
        LOGGER.info("Room closed code=%s reason=%s", code, reason)

    async def shutdown_room(self, room: Room, reason: str) -> None:
        """Shut a room's engine down, which also closes the room via on_ended."""
        await room.engine.shutdown(reason=reason, notify=True)

    def start_sweep(self) -> None:
        """Start the background cleanup task."""
        if self._sweep_task is None or self._sweep_task.done():
            self._sweep_task = asyncio.create_task(self._sweep_loop())

    async def _sweep_loop(self) -> None:
        """Periodically remove ended, empty and abandoned rooms, and persist."""
        while not self._closed:
            await asyncio.sleep(settings.server.sweep_interval_seconds)
            now = time()
            for code, room in list(self.rooms.items()):
                if room.engine.state is GameState.ENDED:
                    await self.remove_room(code, "The room was closed.", notify=True)
                    continue
                connected = any(player.is_connected for player in room.engine.players.values())
                if connected:
                    room.last_active = now
                    continue
                if room.engine.state in _PRE_GAME_STATES:
                    timeout = settings.server.empty_room_timeout_seconds
                    reason = "房间因无人已自动关闭。"
                else:
                    timeout = settings.server.abandoned_room_timeout_seconds
                    reason = "所有玩家断线后，房间已自动关闭。"
                if timeout is None:
                    # Persistent rooms are never removed for being empty.
                    continue
                if now - room.last_active >= timeout:
                    await self.shutdown_room(room, reason)
            self.save_all()

    async def close_all(self) -> None:
        """Stop the sweep, persist every room and close its connections."""
        self._closed = True
        task = self._sweep_task
        if task is not None and task is not asyncio.current_task():
            task.cancel()
            await asyncio.gather(task, return_exceptions=True)
        for code, room in list(self.rooms.items()):
            self.save_room(room)
            room.engine.on_ended = None
            await room.engine.suspend()
            await room.connections.close()
            self.rooms.pop(code, None)

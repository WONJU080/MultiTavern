"""Room registry, invite-code generation and room lifecycle sweeping."""

import asyncio
import hashlib
import hmac
import json
import logging
import secrets
from dataclasses import dataclass
from pathlib import Path
from time import time
from typing import Any, Callable

from api.connections import ConnectionManager
from core.config import settings
from core.schemas import ServerEvent
from logic.engine import GameEngine, restore_engine
from logic.models import GameState
from logic.round_history import RoundHistoryStore

LOGGER = logging.getLogger(__name__)

# Unambiguous uppercase alphabet: no I, L, O, 0 or 1.
CODE_ALPHABET = "ABCDEFGHJKMNPQRSTUVWXYZ23456789"
CODE_GROUPS = (3, 3)

ROOMS_DIR = Path(".rooms")

_PRE_GAME_STATES = frozenset(
    {
        GameState.AWAITING_HOST,
        GameState.SCENARIO_INJECTION,
        GameState.AWAITING_PLAYERS,
    }
)


@dataclass
class Room:
    """One independent game session: engine, connections and activity stamps."""

    code: str
    engine: GameEngine
    connections: ConnectionManager
    created_at: float
    last_active: float


def normalize_invite_code(value: object) -> str:
    """Uppercase and strip separators from a client-supplied invite code."""
    if not isinstance(value, str):
        raise ValueError("'invite_code' must be a string")
    compact = "".join(ch for ch in value.upper() if ch.isalnum())
    if not compact:
        raise ValueError("'invite_code' must contain 1-12 characters")
    if len(compact) > 12:
        raise ValueError("'invite_code' must contain at most 12 characters")
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


def admin_password_matches(digest: object, client_id: str) -> bool:
    """Return whether a digest matches the optional room-creation password."""
    password = settings.server.admin_password
    if not password:
        return True
    if (
        not isinstance(digest, str)
        or len(digest) != 64
        or any(char not in "0123456789abcdef" for char in digest)
    ):
        return False
    expected = hashlib.sha256(f"{password}{client_id}".encode()).hexdigest()
    return hmac.compare_digest(digest, expected)


class RoomRegistry:
    """Own every live room and remove empty or ended rooms."""

    def __init__(self, resolver_factory: Callable[[], Any]) -> None:
        """Initialize the registry with the resolver factory for new engines."""
        self.rooms: dict[str, Room] = {}
        self._resolver_factory = resolver_factory
        self._sweep_task: asyncio.Task | None = None
        self._closed = False

    def generate_code(self) -> str:
        """Return a unique, compact invite code from the unambiguous alphabet."""
        for _ in range(100):
            code = "".join(secrets.choice(CODE_ALPHABET) for _ in range(sum(CODE_GROUPS)))
            if code not in self.rooms:
                return code
        raise RuntimeError("Could not generate a unique invite code")

    def create_room(self, client_id: str, data: dict[str, Any]) -> Room:
        """Create a fresh room with its own engine after the admin check."""
        if not admin_password_matches(data.get("admin_password_digest"), client_id):
            raise ValueError("Invalid admin password.")
        code = self.generate_code()
        connections = ConnectionManager()
        engine = GameEngine(connections, self._resolver_factory())
        room = Room(
            code=code,
            engine=engine,
            connections=connections,
            created_at=time(),
            last_active=time(),
        )
        engine.room_code = format_invite_code(code)
        registry = self

        async def on_ended(reason: str) -> None:
            await registry.remove_room(code, reason, notify=True)

        engine.on_ended = on_ended
        self.rooms[code] = room
        LOGGER.info("Room created code=%s", code)
        return room

    def find_room(self, invite_code: object) -> Room:
        """Return the room for a client-supplied invite code."""
        code = normalize_invite_code(invite_code)
        room = self.rooms.get(code)
        if room is None:
            raise ValueError("Room not found. Check the invite code.")
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
        """Persist a room's state to disk atomically."""
        try:
            ROOMS_DIR.mkdir(parents=True, exist_ok=True)
            data = {"code": room.code, **room.engine.to_persistent_dict()}
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
        """Rebuild every persisted room from disk."""
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
                room = Room(
                    code=code,
                    engine=engine,
                    connections=connections,
                    created_at=time(),
                    last_active=time(),
                )
                registry = self

                async def on_ended(reason: str) -> None:
                    await registry.remove_room(code, reason, notify=True)

                engine.on_ended = on_ended
                self.rooms[code] = room
                LOGGER.info("Room restored code=%s state=%s", code, engine.state.name)
            except Exception as exc:  # noqa: BLE001 - a corrupt file must not block startup
                LOGGER.warning("Could not restore room %s: %s", path.name, exc)

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
                    reason = "The room was closed after being empty."
                else:
                    timeout = settings.server.abandoned_room_timeout_seconds
                    reason = "The room was closed after everyone disconnected."
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

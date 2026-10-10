"""Per-room API credentials: modes, borrowing caps, encryption and recovery."""

import asyncio
from pathlib import Path
from uuid import uuid4

import pytest

from core.config import settings
from core.secrets import decrypt_text
from logic.llm_manager import LLMContextManager
from logic.rooms import RoomRegistry, _borrow_limit_message, _probe_llm_key
from test_engine import FakeResolver
from test_priority_one_transport import create_room, receive_until


async def _allow_all_keys(config) -> None:
    """Deterministic stand-in for the provider probe in offline tests."""
    return None


@pytest.fixture
def offline_probe(monkeypatch):
    """Keep every create_room call offline."""
    monkeypatch.setattr("logic.rooms._probe_llm_key", _allow_all_keys)


def _create(registry, client_id, name, llm=None):
    data = {"name": name}
    if llm is not None:
        data["llm"] = llm
    return asyncio.run(registry.create_room(client_id, data))


def test_borrow_is_the_default_and_ignores_admin_password(offline_probe):
    """A payload without llm borrows the server API; creation has no gate."""
    settings.server.admin_password = "secret-admin"
    registry = RoomRegistry(FakeResolver)
    room = _create(registry, "host", "Host")
    assert room.llm_meta is None
    assert registry.borrowed_rooms_in_use() == 1


def test_own_key_is_encrypted_at_rest_and_restored(offline_probe):
    """The host key never reaches disk as plaintext and round-trips on restore."""
    registry = RoomRegistry(LLMContextManager)
    room = _create(
        registry,
        "host",
        "Host",
        {"mode": "own", "api_key": "sk-plain-secret", "model_name": "deepseek-chat"},
    )
    assert room.llm_meta is not None
    assert decrypt_text(room.llm_meta["key_cipher"]) == "sk-plain-secret"
    assert room.engine.resolver.llm.api_key == "sk-plain-secret"
    registry.save_room(room)
    text = (Path(".rooms") / f"{room.code}.json").read_text(encoding="utf-8")
    assert "sk-plain-secret" not in text
    restored_registry = RoomRegistry(LLMContextManager)
    restored_registry.restore_rooms()
    restored = restored_registry.rooms[room.code]
    assert restored.engine.resolver.llm.api_key == "sk-plain-secret"
    assert restored.engine.resolver.key_required is False
    assert restored_registry.borrowed_rooms_in_use() == 0


def test_legacy_room_file_without_llm_is_borrowed(offline_probe):
    """Rooms persisted before per-room keys count as borrowed and use the server API."""
    registry = RoomRegistry(LLMContextManager)
    room = _create(registry, "host", "Host")
    registry.save_room(room)
    assert "llm" not in (Path(".rooms") / f"{room.code}.json").read_text(encoding="utf-8")
    restored_registry = RoomRegistry(LLMContextManager)
    restored_registry.restore_rooms()
    restored = restored_registry.rooms[room.code]
    assert restored.llm_meta is None
    assert restored.engine.resolver.llm.api_key == settings.llm.api_key
    assert restored_registry.borrowed_rooms_in_use() == 1


def test_borrowed_rooms_are_capped_with_a_clear_message(offline_probe):
    """Borrowing the server API stops at the configured limit."""
    settings.server.borrowed_room_limit = 2
    registry = RoomRegistry(FakeResolver)
    _create(registry, "host-a", "A")
    _create(registry, "host-b", "B")
    with pytest.raises(ValueError) as excinfo:
        _create(registry, "host-c", "C")
    assert str(excinfo.value) == "已有两个房间正在使用，请选用自己的api"
    assert str(excinfo.value) == _borrow_limit_message(2)


def test_own_key_rooms_do_not_consume_borrow_slots(offline_probe):
    """A host-supplied key leaves the borrowed slot pool untouched."""
    settings.server.borrowed_room_limit = 1
    registry = RoomRegistry(FakeResolver)
    _create(registry, "own", "Own", {"mode": "own", "api_key": "sk-one"})
    _create(registry, "borrow", "Borrow")
    with pytest.raises(ValueError, match="已有"):
        _create(registry, "extra", "Extra")


def test_borrowing_can_be_disabled(offline_probe):
    """A zero limit refuses every borrowed room."""
    settings.server.borrowed_room_limit = 0
    registry = RoomRegistry(FakeResolver)
    with pytest.raises(ValueError, match="关闭借用"):
        _create(registry, "host", "Host")


def test_room_limit_caps_total_rooms(offline_probe):
    """max_rooms caps the whole registry regardless of credential mode."""
    settings.server.max_rooms = 1
    registry = RoomRegistry(FakeResolver)
    _create(registry, "host", "Host")
    with pytest.raises(ValueError, match="上限"):
        _create(registry, "other", "Other")


def test_endpoint_allowlist_and_scheme_are_enforced(offline_probe):
    """Explicit endpoints must be http(s) and inside the optional allowlist."""
    settings.server.allowed_llm_hosts = ["api.allowed.com"]
    registry = RoomRegistry(FakeResolver)
    with pytest.raises(ValueError, match="endpoint"):
        _create(
            registry,
            "blocked",
            "Blocked",
            {"mode": "own", "api_key": "sk", "endpoint": "https://api.evil.com/v1"},
        )
    with pytest.raises(ValueError, match="http"):
        _create(
            registry,
            "scheme",
            "Scheme",
            {"mode": "own", "api_key": "sk", "endpoint": "ftp://api.allowed.com/v1"},
        )
    room = _create(
        registry,
        "allowed",
        "Allowed",
        {"mode": "own", "api_key": "sk", "endpoint": "https://api.allowed.com/v1"},
    )
    assert room.llm_meta["endpoint"] == "https://api.allowed.com/v1"


def test_undecryptable_key_blocks_the_room_until_the_host_replaces_it(offline_probe):
    """A lost keyring must never fall back to the server's own credentials."""
    registry = RoomRegistry(LLMContextManager)
    room = _create(registry, "host", "Host", {"mode": "own", "api_key": "sk-original"})
    asyncio.run(room.engine.host_join("host", {"name": "Host"}))
    registry.save_room(room)
    Path(settings.server.keyring_path).unlink()
    restored_registry = RoomRegistry(LLMContextManager)
    restored_registry.restore_rooms()
    restored = restored_registry.rooms[room.code]
    assert restored.engine.resolver.key_required is True
    with pytest.raises(ValueError, match="房主"):
        asyncio.run(restored_registry.provide_room_key(restored, "intruder", {"api_key": "sk-x"}))
    asyncio.run(restored_registry.provide_room_key(restored, "host", {"api_key": "sk-new"}))
    assert restored.engine.resolver.key_required is False
    assert restored.engine.resolver.llm.api_key == "sk-new"
    assert decrypt_text(restored.llm_meta["key_cipher"]) == "sk-new"


def test_provide_key_is_rejected_on_borrowed_rooms(offline_probe):
    """Borrowed rooms never need (or accept) a host key."""
    registry = RoomRegistry(LLMContextManager)
    room = _create(registry, "host", "Host")
    asyncio.run(room.engine.host_join("host", {"name": "Host"}))
    with pytest.raises(ValueError):
        asyncio.run(registry.provide_room_key(room, "host", {"api_key": "sk-x"}))


def test_probe_rejects_only_explicit_auth_failures(monkeypatch):
    """401/403 fail fast; unreachable providers do not masquerade as bad keys."""
    import logic.rooms as rooms_module

    class Response:
        status_code = 401

    class Client:
        def __init__(self, **kwargs):
            pass

        async def __aenter__(self):
            return self

        async def __aexit__(self, *args):
            return False

        async def get(self, url, headers=None):
            return Response()

    config = settings.llm.model_copy(
        update={"api_key": "bad", "provider": "compatible", "endpoint": "https://api.test/v1"}
    )
    monkeypatch.setattr(rooms_module.httpx, "AsyncClient", Client)
    with pytest.raises(ValueError, match="401"):
        asyncio.run(_probe_llm_key(config))

    class FailingClient(Client):
        async def get(self, url, headers=None):
            raise rooms_module.httpx.ConnectError("offline")

    monkeypatch.setattr(rooms_module.httpx, "AsyncClient", FailingClient)
    asyncio.run(_probe_llm_key(config))


class KeyAwareResolver(FakeResolver):
    """Fake resolver that tracks per-room credentials like the real manager."""

    def __init__(self) -> None:
        super().__init__()
        self.key_required = False
        self.llm_config = None

    def apply_llm_config(self, config) -> None:
        self.llm_config = config
        self.key_required = False

    def require_key(self) -> None:
        self.key_required = True


def test_asgi_provide_key_flow(offline_probe):
    """The host can restore a blocked room's key over the WebSocket gateway."""
    from fastapi.testclient import TestClient

    from api.server import create_app

    app = create_app(KeyAwareResolver)
    host_id = str(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{host_id}") as host:
            create_room(host, host_id, "Host", llm={"mode": "own", "api_key": "sk-first"})
            receive_until(host, "auth_ok")
            room = next(iter(app.state.registry.rooms.values()))
            assert room.engine.resolver.llm_config.api_key == "sk-first"
            room.engine.resolver.require_key()
            host.send_json({"event_type": "provide_key", "data": {"api_key": "sk-second"}})
            updated = receive_until(host, "key_updated")
            assert updated["payload"]["msg"]
            assert room.engine.resolver.llm_config.api_key == "sk-second"
            assert room.engine.resolver.key_required is False

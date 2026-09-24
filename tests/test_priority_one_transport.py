"""Real ASGI WebSocket room routing tests with no model/network calls."""

import asyncio
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient
from starlette.websockets import WebSocketDisconnect

from api.server import ConnectionManager, create_app
from core.config import settings
from core.schemas import ServerEvent
from logic.rooms import normalize_invite_code
from test_engine import FakeResolver, password_digest


def receive_until(socket, kind, predicate=lambda event: True):
    """Receive events until one of the given kind and predicate arrives."""
    for _ in range(30):
        event = socket.receive_json()
        if event["type"] == kind and predicate(event):
            return event
    raise AssertionError(f"Did not receive {kind}")


def create_room(socket, client_id, name="Host", admin_password=None):
    """Send a create_room message for a client."""
    data = {"name": name}
    if admin_password:
        data["admin_password_digest"] = password_digest(admin_password, client_id)
    socket.send_json({"event_type": "create_room", "data": data})


def join_room(socket, client_id, invite_code, name="Arxs", character="Arxs", reconnect_token=None):
    """Send a join_room message for a client."""
    socket.send_json(
        {
            "event_type": "join_room",
            "data": {
                "name": name,
                "invite_code": invite_code,
                "character": character,
                "reconnect_token": reconnect_token,
            },
        }
    )


def test_unauthenticated_socket_cannot_receive_broadcasts_or_take_over_identity():
    """Pending sockets cannot chat, and a shared client id cannot take over a room."""
    app = create_app(FakeResolver)
    host_id, pending_id = str(uuid4()), str(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{host_id}") as host:
            create_room(host, host_id)
            code = receive_until(host, "auth_ok")["payload"]["invite_code"]
            with client.websocket_connect(f"/ws/{pending_id}") as pending:
                host.send_json({"event_type": "chat", "data": {"message": "PRIVATE PARTY CHAT"}})
                receive_until(host, "chat_echo")
                pending.send_json({"event_type": "chat", "data": {"message": "Probe"}})
                # A broadcast queued before the error would be a data leak.
                assert pending.receive_json() == {
                    "type": "error",
                    "payload": {"msg": "Create or join a room before sending game messages."},
                }
            with client.websocket_connect(f"/ws/{host_id}") as impostor:
                join_room(impostor, host_id, code, "Impostor", "Host")
                assert "reconnect token" in impostor.receive_json()["payload"]["msg"]
                impostor.send_json({"event_type": "end_game", "data": {}})
                assert impostor.receive_json()["type"] == "error"
                host.send_json(
                    {"event_type": "chat", "data": {"message": "Host still owns socket"}}
                )
                assert (
                    receive_until(host, "chat_echo")["payload"]["chat"] == "Host still owns socket"
                )
                engine = app.state.registry.rooms[normalize_invite_code(code)].engine
                assert engine.players[host_id].is_connected


def test_reconnect_token_reclaims_an_existing_client_id():
    """A private reconnect token alone can reclaim an existing client id."""
    app = create_app(FakeResolver)
    host_id = str(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{host_id}") as host:
            create_room(host, host_id)
            auth_ok = receive_until(host, "auth_ok")["payload"]
            code, token = auth_ok["invite_code"], auth_ok["reconnect_token"]
            host.send_json(
                {
                    "event_type": "scenario_init",
                    "data": {
                        "scenario": "A cabin.",
                        "characters": [{"name": "Host"}],
                        "host_character": "Host",
                    },
                }
            )
            receive_until(host, "scenario_ready")
            with client.websocket_connect(f"/ws/{host_id}") as replacement:
                join_room(replacement, host_id, code, "Host", "Host")
                error = replacement.receive_json()
                assert "reconnect token" in error["payload"]["msg"]
                join_room(replacement, host_id, code, "Host", "Host", token)
                receive_until(replacement, "auth_ok")
                replacement.send_json({"event_type": "chat", "data": {"message": "Reconnected"}})
                receive_until(replacement, "chat_echo")
                engine = app.state.registry.rooms[normalize_invite_code(code)].engine
                assert engine.players[host_id].is_connected
                assert not engine.players[host_id].return_pending


def test_invalid_auth_attempt_limit_includes_malformed_json():
    """The invalid room-entry attempt limit includes malformed JSON."""
    settings.server.max_auth_attempts = 2
    with TestClient(create_app(FakeResolver)) as client:
        with client.websocket_connect(f"/ws/{uuid4()}") as socket:
            socket.send_text("{")
            assert socket.receive_json()["type"] == "error"
            socket.send_text("{")
            assert socket.receive_json()["type"] == "error"
            with pytest.raises(WebSocketDisconnect) as exc:
                socket.receive_json()
            assert exc.value.code == 1008


def test_pending_connection_cap_and_deadline():
    """The pending connection cap and deadline still apply."""
    settings.server.max_pending_connections = 1
    settings.server.auth_timeout_seconds = 0.1
    app = create_app(FakeResolver)
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{uuid4()}") as idle:
            with pytest.raises(WebSocketDisconnect):
                with client.websocket_connect(f"/ws/{uuid4()}"):
                    pass
            with pytest.raises(WebSocketDisconnect) as exc:
                idle.receive_json()
            assert exc.value.code == 1008


def test_room_creation_requires_admin_password_when_configured():
    """A configured admin password guards room creation."""
    settings.server.admin_password = "secret-admin"
    app = create_app(FakeResolver)
    client_id = str(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{client_id}") as socket:
            create_room(socket, client_id, admin_password="wrong")
            assert socket.receive_json()["payload"]["msg"] == "Invalid admin password."
            create_room(socket, client_id, admin_password="secret-admin")
            assert receive_until(socket, "auth_ok")["payload"]["is_host"]
            with client.websocket_connect(f"/ws/{uuid4()}") as other:
                create_room(other, str(uuid4()))
                assert other.receive_json()["payload"]["msg"] == "Invalid admin password."


def test_manager_pending_promotion_and_old_disconnect_do_not_affect_replacement():
    """Pending promotion and old disconnect do not affect a replacement."""

    class Socket:
        """Minimal fake WebSocket for the manager."""

        def __init__(self):
            """Initialize the fake socket."""
            self.messages = []
            self.closed = False

        async def accept(self):
            """Accept the socket."""
            pass

        async def send_text(self, text):
            """Record a sent text."""
            self.messages.append(text)

        async def close(self, code=1000):
            """Mark the socket closed."""
            self.closed = True

    async def run():
        manager = ConnectionManager()
        old, new = Socket(), Socket()
        await manager.connect("one", old)
        manager.promote("one", old)
        await manager.connect("one", new)
        await manager.broadcast_global(ServerEvent(type="chat_echo", payload={"chat": "hello"}))
        assert len(old.messages) == 1 and new.messages == [] and not old.closed
        assert manager.promote("one", new) is old
        assert not manager.owns("one", old)
        assert not await manager.disconnect("one", old)
        assert manager.owns("one", new)
        await manager.close()
        assert new.closed and not manager.pending

    asyncio.run(run())


@pytest.mark.parametrize("rejoin_host", [False, True])
def test_active_game_reconnect_restores_original_player_with_private_proof(rejoin_host):
    """Reopened tabs reuse the saved ID/token; the invite code alone cannot take over."""
    app = create_app(FakeResolver)
    host_id, player_id = str(uuid4()), str(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{host_id}") as host:
            create_room(host, host_id)
            host_auth = receive_until(host, "auth_ok")["payload"]
            code, host_token = host_auth["invite_code"], host_auth["reconnect_token"]
            host.send_json(
                {
                    "event_type": "scenario_init",
                    "data": {
                        "scenario": "A cabin.",
                        "characters": [{"name": "Host"}, {"name": "Arxs"}],
                        "host_character": "Host",
                    },
                }
            )
            receive_until(host, "scenario_ready")
            with client.websocket_connect(f"/ws/{player_id}") as player:
                join_room(player, player_id, code, "Arxs", "Arxs")
                player_token = receive_until(player, "auth_ok")["payload"]["reconnect_token"]
                host.send_json({"event_type": "start_game", "data": {}})
                receive_until(player, "turn_directive")
                if rejoin_host:
                    original, observer = host, player
                    identity, name, character, token = host_id, "Host", "Host", host_token
                else:
                    original, observer = player, host
                    identity, name, character, token = player_id, "Arxs", "Arxs", player_token
                original.close()
                receive_until(
                    observer,
                    "system_msg",
                    lambda event: (event["payload"]["msg"] == f"{name} disconnected."),
                )
                newcomer_id = str(uuid4())
                with client.websocket_connect(f"/ws/{newcomer_id}") as newcomer:
                    join_room(newcomer, newcomer_id, code, "Newcomer", character)
                    assert "already been claimed" in newcomer.receive_json()["payload"]["msg"]
                with client.websocket_connect(f"/ws/{identity}") as recovered:
                    join_room(recovered, identity, code, name, character, "incorrect-token")
                    assert "reconnect token" in recovered.receive_json()["payload"]["msg"]
                    join_room(recovered, identity, code, name, character, token)
                    snapshot = receive_until(recovered, "auth_ok")["payload"]
                    assert snapshot["client_id"] == identity
                    assert snapshot["name"] == name
                    assert snapshot["character"] == character
                    assert snapshot["is_host"] is rejoin_host
                    assert snapshot["round_number"] == 1
                    assert snapshot["invite_code"] == code
                    engine = app.state.registry.rooms[normalize_invite_code(code)].engine
                    assert len(engine.players) == 2
                    assert engine.players[identity].is_connected
                    recovered.send_json({"event_type": "chat", "data": {"message": "I'm back"}})
                    assert receive_until(recovered, "chat_echo")["payload"]["name"] == name

"""Regressions for the mid-round eviction crash and gateway pending-socket leak."""

import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient

from api.server import create_app

from test_engine import FakeResolver, build_started_game
from test_priority_one_transport import join_room, receive_until


def test_evicting_the_active_player_never_leaves_a_dangling_turn(tmp_path):
    """A same-name join that evicts the active player must not crash the turn."""

    async def run():
        engine, sender, _ = await build_started_game(tmp_path)
        assert engine.active_player_id == "host"
        # Simulate the host having gone offline without a disconnect handler
        # having rotated the turn yet (e.g. within the departure grace).
        engine.players["host"].is_connected = False
        await engine.player_join("p3", {"name": "Host", "character": "Host"})
        assert "host" not in engine.players
        assert "host" not in engine.turn_queue
        assert "host" not in engine.join_order
        assert engine.active_player_id == "player"
        directive = sender.events_of_type("turn_directive")[-1]
        assert directive.payload["active_player_id"] == "player"
        await engine.shutdown()

    asyncio.run(run())


def test_mid_round_same_name_join_cannot_crash_when_turn_dangles(tmp_path):
    """The pending-joiner branch must survive an evicted active player mid-round."""

    async def run():
        engine, sender, _ = await build_started_game(tmp_path)
        # Simulate an in-flight round: another player has acted while the host
        # (still the active player) has silently gone offline.
        engine.round_buffer["player"] = "Keeps watch"
        engine.players["host"].is_connected = False
        await engine.player_join("p3", {"name": "Host", "character": "Host"})
        assert engine.active_player_id is None
        assert "p3" in engine.pending_players
        assert "p3" not in engine.turn_queue
        # The round resolves without the evicted player's input.
        await engine.wait_for_inference()
        assert "p3" in engine.players
        assert "host" not in engine.players
        await engine.shutdown()

    asyncio.run(run())


def test_rejected_join_does_not_leak_gateway_pending_sockets():
    """A failed room join must not leave the socket in the gateway pending set."""

    app = create_app(FakeResolver)
    client_id = str(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{client_id}") as socket:
            join_room(socket, client_id, "NOPE-000", "Arxs", "Arxs")
            error = receive_until(socket, "error")
            assert "Room not found" in error["payload"]["msg"]
        # The socket has closed; the pending set must be empty again.
        assert app.state.manager.pending == set()


def test_authenticated_join_releases_gateway_pending_sockets():
    """Successful authentication promotes the socket out of the pending set."""

    app = create_app(FakeResolver)
    host_id = str(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{host_id}") as host:
            from test_priority_one_transport import create_room

            create_room(host, host_id)
            receive_until(host, "auth_ok")
            # auth_ok can arrive before the server finishes its post-auth cleanup;
            # a round-trip guarantees the release has run.
            host.send_json({"event_type": "chat", "data": {"message": "hi"}})
            receive_until(host, "chat_echo")
            assert app.state.manager.pending == set()
        assert app.state.manager.pending == set()

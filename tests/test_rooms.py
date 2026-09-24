"""Multi-room registry, invite codes, isolation and lifecycle tests."""

import asyncio
import re
import time
from pathlib import Path
from uuid import uuid4

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from core.config import settings
from logic.engine import GameEngine, GameState
from logic.rooms import RoomRegistry, format_invite_code, normalize_invite_code
from logic.transcript import GameTranscript
from test_engine import FakeResolver, FakeSender, payload
from test_priority_one_transport import create_room, join_room, receive_until

CODE_PATTERN = re.compile(r"[ABCDEFGHJKMNPQRSTUVWXYZ23456789]{6}")


def test_normalize_invite_code():
    """Invite codes normalize separators and casing, and reject invalid input."""
    assert normalize_invite_code("abc-123") == "ABC123"
    assert normalize_invite_code(" abc 123 ") == "ABC123"
    assert normalize_invite_code("AB C 12 3") == "ABC123"
    with pytest.raises(ValueError):
        normalize_invite_code("")
    with pytest.raises(ValueError):
        normalize_invite_code("---")
    with pytest.raises(ValueError):
        normalize_invite_code("x" * 13)
    with pytest.raises(ValueError):
        normalize_invite_code(42)


def test_invite_code_formatting_round_trips():
    """Compact codes format for display and normalize back to the lookup key."""
    assert format_invite_code("K68WPD") == "K68-WPD"
    assert normalize_invite_code(format_invite_code("K68WPD")) == "K68WPD"


def test_generated_codes_are_unique_and_formatted():
    """Generated codes use the unambiguous alphabet and stay unique."""
    registry = RoomRegistry(FakeResolver)
    codes = {registry.generate_code() for _ in range(50)}
    assert len(codes) == 50
    for code in codes:
        assert CODE_PATTERN.fullmatch(code)


def test_rooms_are_isolated_between_concurrent_games():
    """Chat and game state never leak across concurrent rooms."""
    app = create_app(FakeResolver)
    a_id, b_id = str(uuid4()), str(uuid4())
    with TestClient(app) as client:
        with (
            client.websocket_connect(f"/ws/{a_id}") as host_a,
            client.websocket_connect(f"/ws/{b_id}") as host_b,
        ):
            create_room(host_a, a_id, "HostA")
            create_room(host_b, b_id, "HostB")
            code_a = receive_until(host_a, "auth_ok")["payload"]["invite_code"]
            code_b = receive_until(host_b, "auth_ok")["payload"]["invite_code"]
            assert code_a != code_b
            assert len(app.state.registry.rooms) == 2

            host_a.send_json({"event_type": "chat", "data": {"message": "Only in A"}})
            assert receive_until(host_a, "chat_echo")["payload"]["chat"] == "Only in A"
            host_b.send_json({"event_type": "chat", "data": {"message": "Only in B"}})
            # FIFO order: any leak from room A would arrive first.
            assert receive_until(host_b, "chat_echo")["payload"]["chat"] == "Only in B"

            engine_a = app.state.registry.rooms[normalize_invite_code(code_a)].engine
            engine_b = app.state.registry.rooms[normalize_invite_code(code_b)].engine
            assert engine_a is not engine_b
            assert engine_a.players[a_id].name == "HostA"
            assert engine_b.players[b_id].name == "HostB"


def test_same_name_allowed_across_rooms_but_not_within_one():
    """Player names are scoped per room."""
    app = create_app(FakeResolver)
    a_id, b_id, p1_id, p2_id = (str(uuid4()) for _ in range(4))
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{a_id}") as host_a:
            create_room(host_a, a_id, "HostA")
            code_a = receive_until(host_a, "auth_ok")["payload"]["invite_code"]
            host_a.send_json(
                {
                    "event_type": "scenario_init",
                    "data": {
                        "scenario": "A gate.",
                        "characters": [{"name": "HostA"}, {"name": "Arxs"}],
                        "host_character": "HostA",
                    },
                }
            )
            receive_until(host_a, "scenario_ready")
            with client.websocket_connect(f"/ws/{b_id}") as host_b:
                create_room(host_b, b_id, "HostB")
                code_b = receive_until(host_b, "auth_ok")["payload"]["invite_code"]
                host_b.send_json(
                    {
                        "event_type": "scenario_init",
                        "data": {
                            "scenario": "A forest.",
                            "characters": [{"name": "HostB"}, {"name": "Arxs"}],
                            "host_character": "HostB",
                        },
                    }
                )
                receive_until(host_b, "scenario_ready")
                with (
                    client.websocket_connect(f"/ws/{p1_id}") as player_one,
                    client.websocket_connect(f"/ws/{p2_id}") as player_two,
                ):
                    join_room(player_one, p1_id, code_a, "Arxs", "Arxs")
                    receive_until(player_one, "auth_ok")
                    join_room(player_two, p2_id, code_b, "Arxs", "Arxs")
                    receive_until(player_two, "auth_ok")
                    with client.websocket_connect(f"/ws/{uuid4()}") as duplicate:
                        join_room(duplicate, str(uuid4()), code_a, "arxs", "Arxs")
                        assert "already in use" in duplicate.receive_json()["payload"]["msg"]


def test_joining_an_unknown_room_reports_a_clear_error():
    """Unknown invite codes are rejected with a clear message."""
    app = create_app(FakeResolver)
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{uuid4()}") as socket:
            join_room(socket, str(uuid4()), "ZZZ-ZZZ", "Arxs", "Arxs")
            assert "Room not found" in socket.receive_json()["payload"]["msg"]


def test_cast_cap_is_enforced_per_room():
    """Each room enforces its own cast cap."""
    settings.server.max_characters = 2
    app = create_app(FakeResolver)
    host_id = str(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{host_id}") as host:
            create_room(host, host_id, "Host")
            receive_until(host, "auth_ok")
            host.send_json(
                {
                    "event_type": "scenario_init",
                    "data": {
                        "scenario": "A gate.",
                        "characters": [{"name": "A"}, {"name": "B"}, {"name": "C"}],
                        "host_character": "A",
                    },
                }
            )
            assert "At most 2 characters" in receive_until(host, "error")["payload"]["msg"]
            host.send_json(
                {
                    "event_type": "scenario_init",
                    "data": {
                        "scenario": "A gate.",
                        "characters": [{"name": "A"}, {"name": "B"}],
                        "host_character": "A",
                    },
                }
            )
            receive_until(host, "scenario_ready")


def test_host_ending_the_game_closes_the_room():
    """Ending a game removes the room and notifies its members."""
    app = create_app(FakeResolver)
    host_id = str(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{host_id}") as host:
            create_room(host, host_id, "Host")
            code = receive_until(host, "auth_ok")["payload"]["invite_code"]
            host.send_json(
                {
                    "event_type": "scenario_init",
                    "data": {
                        "scenario": "A gate.",
                        "characters": [{"name": "Host"}],
                        "host_character": "Host",
                    },
                }
            )
            receive_until(host, "scenario_ready")
            host.send_json({"event_type": "start_game", "data": {}})
            receive_until(host, "turn_directive")
            host.send_json({"event_type": "end_game", "data": {}})
            assert receive_until(host, "game_ended")["payload"]["msg"] == "The host ended the game."
            assert (
                receive_until(host, "room_closed")["payload"]["msg"] == "The host ended the game."
            )
            assert normalize_invite_code(code) not in app.state.registry.rooms


def test_empty_lobby_room_is_swept_after_timeout():
    """An empty pre-game room is removed by the sweep."""
    settings.server.empty_room_timeout_seconds = 0.2
    settings.server.sweep_interval_seconds = 0.05
    app = create_app(FakeResolver)
    host_id = str(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{host_id}") as host:
            create_room(host, host_id, "Host")
            code = receive_until(host, "auth_ok")["payload"]["invite_code"]
        deadline = time.monotonic() + 5
        while (
            normalize_invite_code(code) in app.state.registry.rooms and time.monotonic() < deadline
        ):
            time.sleep(0.05)
        assert normalize_invite_code(code) not in app.state.registry.rooms
        assert not app.state.registry.rooms


def test_persistent_rooms_survive_emptiness_when_timeouts_are_null():
    """Null timeouts disable the sweep so rooms are never removed for being empty."""
    settings.server.empty_room_timeout_seconds = None
    settings.server.abandoned_room_timeout_seconds = None
    settings.server.sweep_interval_seconds = 0.05
    app = create_app(FakeResolver)
    host_id = str(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{host_id}") as host:
            create_room(host, host_id, "Host")
            code = receive_until(host, "auth_ok")["payload"]["invite_code"]
        time.sleep(0.4)
        assert normalize_invite_code(code) in app.state.registry.rooms
        assert app.state.registry.rooms


def test_host_can_close_a_lobby_room(tmp_path: Path):
    """The host can dissolve the room from any state via close_room."""

    async def run():
        sender = FakeSender()
        engine = GameEngine(sender, FakeResolver())
        engine.transcript = GameTranscript(tmp_path)
        await engine.host_join("host", {"name": "Host"})
        with pytest.raises(ValueError, match="not accepting new players"):
            await engine.player_join("p1", {"name": "One", "character": "Ghost"})
        await engine.process_payload("host", payload("close_room"))
        assert engine.state is GameState.ENDED
        assert sender.events_of_type("game_ended")[-1].payload["msg"] == "The host closed the room."

    asyncio.run(run())

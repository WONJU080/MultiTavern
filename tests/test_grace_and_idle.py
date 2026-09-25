"""Disconnect grace period and no-idle-round regressions."""

import asyncio

from logic.engine import GameState
from test_characters import CAST_TWO, build_engine, set_scenario
from test_engine import payload


def test_disconnect_grace_defers_departure_until_timeout(tmp_path):
    """A brief dropout does not mark a player as departed until the grace ends."""

    async def run():
        engine, sender, _ = await build_engine(tmp_path)
        await set_scenario(engine, CAST_TWO, "Host")
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        assert engine.players["host"].is_connected

        gone = {"flag": False}

        def still_disconnected():
            return gone["flag"]

        engine.schedule_disconnect(
            "host",
            expected_version=engine.players["host"].connection_version,
            still_disconnected=still_disconnected,
            delay=0.2,
        )
        gone["flag"] = True
        await asyncio.sleep(0.05)
        assert engine.players["host"].is_connected
        assert sender.events_of_type("system_msg")[-1].payload["msg"] != "Host disconnected."
        await asyncio.sleep(0.3)
        assert not engine.players["host"].is_connected
        assert sender.events_of_type("system_msg")[-1].payload["msg"] == "Host disconnected."
        await engine.shutdown()

    asyncio.run(run())


def test_reconnecting_within_grace_avoids_departure(tmp_path):
    """A player who returns before the grace expires stays in the room silently."""

    async def run():
        engine, sender, _ = await build_engine(tmp_path)
        await set_scenario(engine, CAST_TWO, "Host")
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        token = engine.players["host"].reconnect_token

        back = {"flag": False}

        def still_disconnected():
            return not back["flag"]

        engine.schedule_disconnect(
            "host",
            expected_version=engine.players["host"].connection_version,
            still_disconnected=still_disconnected,
            delay=0.2,
        )
        back["flag"] = True
        await engine.player_join(
            "host", {"name": "Host", "character": "Host", "reconnect_token": token}
        )
        await asyncio.sleep(0.3)
        assert engine.players["host"].is_connected
        assert sender.events_of_type("system_msg")[-1].payload["msg"] != "Host disconnected."
        await engine.shutdown()

    asyncio.run(run())


def test_all_idle_round_never_auto_advances(tmp_path):
    """No round resolves when every acting player is only an idle placeholder."""

    async def run():
        engine, _, resolver = await build_engine(tmp_path)
        await set_scenario(engine, [{"name": "Solo"}], "")
        await engine.player_join("p1", {"name": "One", "character": "Solo"})
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        assert engine.active_player_id == "p1"
        await engine.handle_disconnect("p1")
        assert engine.state is GameState.ACTIVE_TURN
        assert engine.round_buffer == {}
        assert resolver.received_actions == []
        await engine.shutdown()

    asyncio.run(run())


def test_reconnecting_player_can_act_after_idle_reset(tmp_path):
    """After an all-idle reset, a returning player can act and resolve the round."""

    async def run():
        engine, _, _ = await build_engine(tmp_path)
        await set_scenario(engine, [{"name": "Solo"}], "")
        await engine.player_join("p1", {"name": "One", "character": "Solo"})
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        token = engine.players["p1"].reconnect_token
        await engine.handle_disconnect("p1")
        assert engine.round_buffer == {}
        await engine.player_join(
            "p1", {"name": "One", "character": "Solo", "reconnect_token": token}
        )
        assert engine.active_player_id == "p1"
        await engine.process_payload("p1", payload("action", action="Look around"))
        await engine.wait_for_inference()
        assert engine.round_counter == 1
        await engine.shutdown()

    asyncio.run(run())


def test_asgi_disconnect_marks_departed_after_grace():
    """Over the real socket path, a gone player is marked departed after the grace."""
    import time
    from uuid import uuid4

    from fastapi.testclient import TestClient

    from api.server import create_app
    from core.config import settings
    from logic.rooms import normalize_invite_code
    from test_engine import FakeResolver
    from test_priority_one_transport import create_room, receive_until

    settings.server.disconnect_grace_seconds = 0.3
    app = create_app(FakeResolver)
    host_id = str(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{host_id}") as host:
            create_room(host, host_id, "Host")
            code = receive_until(host, "auth_ok")["payload"]["invite_code"]
        engine = app.state.registry.rooms[normalize_invite_code(code)].engine
        assert engine.players[host_id].is_connected
        time.sleep(0.6)
        assert not engine.players[host_id].is_connected

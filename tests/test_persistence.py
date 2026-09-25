"""Room persistence across server restarts."""

import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient

from api.server import create_app
from core.config import settings
from logic.engine import restore_engine
from logic.rooms import normalize_invite_code
from test_characters import CAST_TWO, build_engine, set_scenario
from test_engine import FakeResolver, FakeSender, payload
from test_priority_one_lifecycle import ControlledResolver
from test_priority_one_transport import create_room, receive_until


def test_engine_state_round_trips(tmp_path):
    """A started room serializes and rebuilds; a round continues after restore."""

    async def run():
        engine, _, _ = await build_engine(tmp_path)
        await set_scenario(engine, CAST_TWO, "Host")
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        assert engine.state.name == "ACTIVE_TURN"
        assert engine.claims == {"Host": "Host"}

        data = engine.to_persistent_dict()
        restored = restore_engine(data, FakeSender(), ControlledResolver)
        assert restored.state.name == "ACTIVE_TURN"
        assert [c.name for c in restored.cast] == ["Host", "Player"]
        assert restored.claims == {"Host": "Host"}
        assert set(restored.players) == {"host"}
        assert restored.players["host"].character_name == "Host"
        assert not restored.players["host"].is_connected
        assert restored.opening_scenario is not None
        assert restored.round_counter == 0
        # A restored room resumes when a player reconnects and acts.
        token = restored.players["host"].reconnect_token
        await restored.player_join(
            "host", {"name": "Host", "character": "Host", "reconnect_token": token}
        )
        await restored.process_payload("host", payload("action", action="Open the gate"))
        await restored.wait_for_inference()
        assert restored.round_counter == 1
        await engine.shutdown()
        await restored.shutdown()

    asyncio.run(run())


def test_registry_persists_and_restores_a_room():
    """Rooms are saved to disk and rebuilt after a simulated restart."""
    settings.server.empty_room_timeout_seconds = None
    settings.server.abandoned_room_timeout_seconds = None
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
        registry = app.state.registry
        key = normalize_invite_code(code)
        assert key in registry.rooms
        registry.save_all()
        registry.rooms.clear()
        registry.restore_rooms()
        assert key in registry.rooms
        restored = registry.rooms[key].engine
        assert restored.state.name == "AWAITING_PLAYERS"
        assert [c.name for c in restored.cast] == ["Host"]
        assert restored.claims == {"Host": "Host"}
        assert restored.players[host_id].reconnect_token

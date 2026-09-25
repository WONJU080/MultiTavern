"""Player-safe round history: records, replay data, capping, and persistence."""

import asyncio
from uuid import uuid4

from fastapi.testclient import TestClient

from api.server import create_app
from core.schemas import DicePlan, RoundResolution
from logic.engine import MAX_ROUND_HISTORY, GameEngine, restore_engine
from logic.round_history import RoundHistoryStore
from logic.transcript import GameTranscript

from test_engine import FakeResolver, FakeSender, build_started_game, payload
from test_priority_one_transport import create_room, join_room, receive_until


class DiceResolver(FakeResolver):
    """Fake resolver that plans one hidden roll for Player."""

    async def plan_dice(self, actions, current_state=""):
        return DicePlan(rolls={name: True for name in actions}, hidden_rolls=["Player"])

    async def generate_resolution(
        self, round_buffer, dice_results=None, hidden_rolls=None, current_time="", event_context=""
    ):
        self.last_dice = dict(dice_results or {})
        return RoundResolution(
            global_narrative="State after.",
            player_resolutions={name: f"{name} done." for name in round_buffer},
        )


async def play_two_rounds(engine):
    """Play two full rounds with both players acting."""
    await engine.process_payload("host", payload("action", action="Opens the gate"))
    await engine.process_payload("player", payload("action", action="Keeps watch"))
    await engine.wait_for_inference()
    await engine.process_payload("host", payload("action", action="Follows"))
    await engine.process_payload("player", payload("action", action="Runs ahead"))
    await engine.wait_for_inference()


def test_round_history_records_public_rounds(tmp_path):
    """Every committed round lands in the history with public fields only."""

    async def run():
        engine, sender, _ = await build_started_game(tmp_path)
        await play_two_rounds(engine)
        assert len(engine.round_history) == 2
        first, second = engine.round_history
        assert first["round_number"] == 1 and second["round_number"] == 2
        assert first["global_narrative"] == "State after round 1."
        assert set(first["player_resolutions"]) == {"Host", "Player"}
        assert set(first["actions"]) == {"Host", "Player"}
        assert first["player_order"] == ["Host", "Player"]
        assert first["game_time"] == ""
        snapshot = engine._snapshot_locked(engine.players["player"])
        assert len(snapshot["round_history"]) == 2
        assert snapshot["round_history"][1]["round_number"] == 2
        await engine.shutdown()

    asyncio.run(run())


def test_round_history_excludes_idle_actions_and_hidden_dice(tmp_path):
    """Disconnect-idle actions and hidden rolls never enter the history."""

    async def run():
        sender = FakeSender()
        resolver = DiceResolver()
        engine = GameEngine(sender, resolver)
        engine.transcript = GameTranscript(tmp_path / "logs")
        await engine.host_join("host", {"name": "Host"})
        await engine.process_payload(
            "host",
            payload(
                "scenario_init",
                scenario="A gate.",
                characters=[{"name": "Host"}, {"name": "Player"}],
                host_character="Host",
            ),
        )
        await engine.wait_for_inference()
        await engine.player_join("player", {"name": "Player", "character": "Player"})
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        await engine.handle_disconnect("player")
        await engine.process_payload("host", payload("action", action="Opens the gate"))
        await engine.wait_for_inference()
        record = engine.round_history[0]
        assert set(record["actions"]) == {"Host"}
        assert "Player" not in record["dice_results"]
        assert "Host" in record["dice_results"]
        await engine.shutdown()

    asyncio.run(run())


def test_round_history_is_capped(tmp_path):
    """The history keeps at most the configured number of recent rounds."""

    async def run():
        engine, _, _ = await build_started_game(tmp_path)
        engine.round_history = [{"round_number": index} for index in range(MAX_ROUND_HISTORY)]
        await engine.process_payload("host", payload("action", action="Opens the gate"))
        await engine.process_payload("player", payload("action", action="Keeps watch"))
        await engine.wait_for_inference()
        assert len(engine.round_history) == MAX_ROUND_HISTORY
        assert engine.round_history[0]["round_number"] == 1
        assert engine.round_history[-1]["round_number"] == 1
        assert "global_narrative" in engine.round_history[-1]
        await engine.shutdown()

    asyncio.run(run())


def test_round_history_survives_persistence(tmp_path):
    """Replay data is restored after a room save/load round-trip."""

    async def run():
        engine, sender, _ = await build_started_game(tmp_path)
        await play_two_rounds(engine)
        restored = restore_engine(engine.to_persistent_dict(), sender, FakeResolver)
        assert len(restored.round_history) == 2
        assert restored.round_history[1]["global_narrative"] == "State after round 2."
        await restored.shutdown()

    asyncio.run(run())


def test_old_rooms_without_history_default_to_empty(tmp_path):
    """Rooms saved before the history feature restore without a history."""

    async def run():
        sender = FakeSender()
        engine, _, _ = await build_started_game(tmp_path)
        data = engine.to_persistent_dict()
        data.pop("round_history", None)
        restored = restore_engine(data, sender, FakeResolver)
        assert restored.round_history == []
        await restored.shutdown()

    asyncio.run(run())


def test_history_archive_is_unbounded_and_paginated(tmp_path, monkeypatch):
    """The JSONL archive keeps every round and pages them oldest-first."""

    async def run():
        monkeypatch.chdir(tmp_path)
        engine, _, _ = await build_started_game(tmp_path)
        engine.room_code = "TEST-ROOM"
        for index in range(65):
            await engine.process_payload("host", payload("action", action=f"Act {index + 1}"))
            await engine.process_payload("player", payload("action", action=f"Wait {index + 1}"))
            await engine.wait_for_inference()
        # 65 rounds is below the in-memory tail cap; the archive itself is unbounded.
        assert len(engine.round_history) == 65
        store = engine.history_store
        compact = engine.room_code.replace("-", "")
        page, has_more = await store.load_before(compact, None)
        assert has_more is True
        assert len(page) == 30
        assert page[-1]["round_number"] == 65
        older, has_more = await store.load_before(compact, page[0]["round_number"])
        assert has_more is True
        assert older[-1]["round_number"] == page[0]["round_number"] - 1
        final, has_more = await store.load_before(compact, older[0]["round_number"])
        assert has_more is False
        assert final[0]["round_number"] == 1
        await engine.shutdown()

    asyncio.run(run())


def test_history_archive_tolerates_malformed_lines(tmp_path, monkeypatch):
    """A corrupted archive line never breaks page loading."""

    async def run():
        monkeypatch.chdir(tmp_path)
        engine, _, _ = await build_started_game(tmp_path)
        engine.room_code = "TEST-ROOM"
        await engine.process_payload("host", payload("action", action="Opens the gate"))
        await engine.process_payload("player", payload("action", action="Keeps watch"))
        await engine.wait_for_inference()
        compact = engine.room_code.replace("-", "")
        path = RoundHistoryStore._path(compact)
        with path.open("a", encoding="utf-8") as history_file:
            history_file.write("{not valid json\n")
            history_file.write("\n")
        page, has_more = await engine.history_store.load_before(compact, None)
        assert [record["round_number"] for record in page] == [1]
        assert has_more is False
        await engine.shutdown()

    asyncio.run(run())


def test_history_request_serves_older_rounds_over_the_websocket():
    """Authenticated members can page backwards through the archive."""

    app = create_app(FakeResolver)
    host_id, player_id = str(uuid4()), str(uuid4())
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{host_id}") as host:
            create_room(host, host_id)
            code = receive_until(host, "auth_ok")["payload"]["invite_code"]
            host.send_json(
                {
                    "event_type": "scenario_init",
                    "data": {
                        "scenario": "A gate.",
                        "characters": [{"name": "Host"}, {"name": "Player"}],
                        "host_character": "Host",
                    },
                }
            )
            receive_until(host, "scenario_ready")
            with client.websocket_connect(f"/ws/{player_id}") as player:
                join_room(player, player_id, code, "Player", "Player")
                receive_until(player, "auth_ok")
                host.send_json({"event_type": "start_game", "data": {}})
                receive_until(player, "turn_directive")
                for index in range(2):
                    host.send_json({"event_type": "action", "data": {"action": f"H{index}"}})
                    player.send_json({"event_type": "action", "data": {"action": f"P{index}"}})
                    receive_until(host, "state_update")
                player.send_json({"event_type": "history_request", "data": {"before_round": 2}})
                chunk = receive_until(player, "history_chunk")["payload"]
                assert [record["round_number"] for record in chunk["rounds"]] == [1]
                assert chunk["has_more"] is False


def test_unauthenticated_history_request_is_rejected():
    """Pending sockets cannot read history before joining a room."""

    app = create_app(FakeResolver)
    with TestClient(app) as client:
        with client.websocket_connect(f"/ws/{uuid4()}") as socket:
            socket.send_json({"event_type": "history_request", "data": {}})
            error = socket.receive_json()
            assert error["type"] == "error"
            assert "Create or join a room" in error["payload"]["msg"]


def test_history_archive_is_deleted_when_the_room_closes(tmp_path, monkeypatch):
    """Closing a room removes its history archive alongside the room file."""

    async def run():
        monkeypatch.chdir(tmp_path)
        engine, sender, _ = await build_started_game(tmp_path)
        engine.room_code = "TEST-ROOM"
        await engine.process_payload("host", payload("action", action="Opens the gate"))
        await engine.process_payload("player", payload("action", action="Keeps watch"))
        await engine.wait_for_inference()
        compact = engine.room_code.replace("-", "")
        path = RoundHistoryStore._path(compact)
        assert path.exists()
        RoundHistoryStore.delete(compact)
        assert not path.exists()
        await engine.shutdown()

    asyncio.run(run())

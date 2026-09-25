"""Offline catch-up: missed global events reach returning players."""

import asyncio

from core.schemas import RoundResolution
from logic.engine import restore_engine

from test_engine import FakeResolver, payload
from test_timed_events import started_game_with_events

EVENT = [
    {"name": "午时广播", "day": 1, "minute": 420, "description": "全城广播响起", "public": True}
]


def test_offline_player_receives_missed_events_on_return(tmp_path):
    """Events fired while a player was offline are caught up on their return."""

    async def run():
        engine, _, _ = await started_game_with_events(tmp_path, EVENT)
        token = engine.players["player"].reconnect_token
        await engine.handle_disconnect("player")
        assert engine.players["player"].last_seen_total == 1440 + 390
        await engine.process_payload("host", payload("action", action="继续前进"))
        await engine.wait_for_inference()
        assert engine.game_clock.format() == "Day 1 07:00"
        snapshot = await engine.player_join(
            "player",
            {
                "name": "Player",
                "character": "Player",
                "reconnect_token": token,
            },
        )
        assert snapshot["catchup"]["missed_minutes"] == 30
        assert snapshot["catchup"]["missed_events"] == ["午时广播"]
        assert snapshot["catchup"]["from"] == "Day 1 06:30"
        assert snapshot["catchup"]["to"] == "Day 1 07:00"
        assert engine.players["player"].catchup_notes
        assert "午时广播" in engine.players["player"].catchup_notes[0]
        await engine.shutdown()

    asyncio.run(run())


def test_catchup_note_reaches_the_next_resolution_and_clears(tmp_path):
    """The catch-up annotation is injected once, then consumed."""

    async def run():
        engine, _, resolver = await started_game_with_events(
            tmp_path,
            [
                {
                    "name": "午时广播",
                    "day": 1,
                    "minute": 420,
                    "description": "全城广播响起",
                    "public": False,
                }
            ],
        )
        token = engine.players["player"].reconnect_token
        await engine.handle_disconnect("player")
        await engine.process_payload("host", payload("action", action="继续前进"))
        await engine.wait_for_inference()
        await engine.player_join(
            "player",
            {"name": "Player", "character": "Player", "reconnect_token": token},
        )
        captured = {}

        async def resolve(
            round_buffer, dice_results=None, hidden_rolls=None, current_time="", event_context=""
        ):
            captured["round"] = dict(round_buffer)
            return RoundResolution(
                global_narrative="Done.",
                player_resolutions={name: f"{name} done." for name in round_buffer},
            )

        resolver.generate_resolution = resolve
        await engine.process_payload("host", payload("action", action="守夜"))
        await engine.process_payload("player", payload("action", action="跟上队伍"))
        await engine.wait_for_inference()
        assert "offline while the in-game clock advanced" in captured["round"]["Player"]
        assert "午时广播" in captured["round"]["Player"]
        assert engine.players["player"].catchup_notes == []
        await engine.shutdown()

    asyncio.run(run())


def test_return_without_missed_events_carries_only_duration(tmp_path):
    """A return with no events missed still reports the offline duration."""

    async def run():
        engine, _, _ = await started_game_with_events(tmp_path, [])
        token = engine.players["player"].reconnect_token
        await engine.handle_disconnect("player")
        await engine.process_payload("host", payload("action", action="继续前进"))
        await engine.wait_for_inference()
        snapshot = await engine.player_join(
            "player",
            {"name": "Player", "character": "Player", "reconnect_token": token},
        )
        assert snapshot["catchup"]["missed_events"] == []
        assert snapshot["catchup"]["missed_minutes"] == 30
        assert engine.players["player"].catchup_notes == []
        await engine.shutdown()

    asyncio.run(run())


def test_last_seen_survives_persistence(tmp_path):
    """The per-player clock cursor is restored after a restart."""

    async def run():
        engine, sender, _ = await started_game_with_events(tmp_path, [])
        await engine.handle_disconnect("player")
        restored = restore_engine(engine.to_persistent_dict(), sender, FakeResolver)
        assert restored.players["player"].last_seen_total == 1440 + 390
        await restored.shutdown()

    asyncio.run(run())

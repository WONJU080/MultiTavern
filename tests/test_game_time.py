"""Authoritative in-game clock: config parsing, advancement, and persistence."""

import asyncio
from pathlib import Path

import pytest

from core.schemas import RoundResolution, TimeConfig, TimeRule
from logic.engine import GameEngine, restore_engine
from logic.game_clock import GameClock
from logic.lobby import parse_time_config, parse_time_rules, render_time_block
from logic.models import GameState
from logic.transcript import GameTranscript

from test_engine import FakeResolver, FakeSender, build_started_game, payload


def test_game_clock_formats_and_rolls_over_days():
    """The clock formats Day HH:MM and rolls over at midnight."""
    clock = GameClock(day=1, minute=390)
    assert clock.format() == "Day 1 06:30"
    clock.add_minutes(40)
    assert clock.format() == "Day 1 07:10"
    clock.add_minutes(18 * 60)
    assert clock.format() == "Day 2 01:10"
    with pytest.raises(ValueError):
        clock.add_minutes(-5)


def test_time_config_and_rules_validation():
    """Malformed clock configuration is rejected with clear errors."""
    config = parse_time_config(
        {"enabled": True, "start_day": 3, "start_minute": 480, "max_elapsed_minutes": 500}
    )
    assert config.enabled and config.start_day == 3 and config.start_minute == 480
    assert config.max_elapsed_minutes == 500
    with pytest.raises(ValueError):
        parse_time_config({"enabled": "yes"})
    with pytest.raises(ValueError):
        parse_time_config({"start_minute": 1440})
    rules = parse_time_rules([{"activity": "吃饭", "minutes_min": 20, "minutes_max": 30}])
    assert rules == [TimeRule(activity="吃饭", minutes_min=20, minutes_max=30)]
    with pytest.raises(ValueError):
        parse_time_rules([{"activity": "bogus", "minutes_min": 50, "minutes_max": 10}])
    with pytest.raises(ValueError):
        parse_time_rules("not-a-list")


def test_render_time_block_includes_rules_and_start():
    """The rendered block is backstage material with the start time and rules."""
    block = render_time_block(
        TimeConfig(enabled=True, start_day=1, start_minute=390),
        [TimeRule(activity="吃饭", minutes_min=20, minutes_max=30)],
    )
    assert "Day 1 06:30" in block
    assert "吃饭: 20-30 minutes" in block
    assert render_time_block(TimeConfig(enabled=False), []) == ""


async def started_timed_game(tmp_path: Path, monkeypatch):
    """Build a started game with time tracking enabled and a 30-minute resolver."""
    sender = FakeSender()
    resolver = FakeResolver()
    engine = GameEngine(sender, resolver)
    engine.transcript = GameTranscript(tmp_path / "logs")
    await engine.host_join("host", {"name": "Host"})
    await engine.process_payload(
        "host",
        payload(
            "scenario_init",
            scenario="A gate blocks the road.",
            characters=[{"name": "Host"}, {"name": "Player"}],
            host_character="Host",
            time_config={
                "enabled": True,
                "start_day": 1,
                "start_minute": 390,
                "max_elapsed_minutes": 120,
                "default_elapsed_minutes": 15,
            },
            time_rules=[{"activity": "短交流", "minutes_min": 5, "minutes_max": 15}],
        ),
    )
    await engine.wait_for_inference()
    await engine.player_join("player", {"name": "Player", "character": "Player"})
    await engine.process_payload("host", payload("start_game"))
    await engine.wait_for_inference()
    return engine, sender, resolver


def test_time_config_enables_clock_and_reaches_the_resolver(tmp_path, monkeypatch):
    """Scenario init enables the clock, seeds it, and passes labels to inference."""

    async def run():
        engine, sender, resolver = await started_timed_game(tmp_path, monkeypatch)
        assert engine.time_enabled
        assert engine.game_clock.format() == "Day 1 06:30"
        assert resolver.time_context is not None
        assert "Day 1 06:30" in resolver.time_context
        assert "短交流: 5-15 minutes" in resolver.time_context
        assert resolver.start_time == "Day 1 06:30"
        opening = sender.events_of_type("state_update")[0].payload
        assert opening["game_time"] == "Day 1 06:30"
        await engine.shutdown()

    asyncio.run(run())


def test_round_advances_the_clock_and_broadcasts_it(tmp_path, monkeypatch):
    """Each resolved round adds the validated estimate to the authoritative clock."""

    async def run():
        engine, sender, resolver = await started_timed_game(tmp_path, monkeypatch)
        await engine.process_payload("host", payload("action", action="Opens the gate"))
        await engine.process_payload("player", payload("action", action="Keeps watch"))
        await engine.wait_for_inference()
        assert resolver.resolution_time == "Day 1 06:30"
        assert engine.game_clock.format() == "Day 1 07:00"
        update = sender.events_of_type("state_update")[-1].payload
        assert update["game_time"] == "Day 1 07:00"
        assert update["time_elapsed_minutes"] == 30
        snapshot = engine._snapshot_locked(engine.players["player"])
        assert snapshot["game_time"] == "Day 1 07:00"
        await engine.shutdown()

    asyncio.run(run())


def test_missing_estimate_uses_the_default(tmp_path, monkeypatch):
    """A resolution without time_elapsed_minutes advances by the configured default."""

    async def run():
        engine, sender, resolver = await started_timed_game(tmp_path, monkeypatch)

        async def resolve(
            actions, dice_results=None, hidden_rolls=None, current_time="", event_context=""
        ):
            return RoundResolution(
                global_narrative="Done.",
                player_resolutions={name: f"{name} done." for name in actions},
            )

        resolver.generate_resolution = resolve
        await engine.process_payload("host", payload("action", action="Wait"))
        await engine.process_payload("player", payload("action", action="Wait too"))
        await engine.wait_for_inference()
        assert engine.game_clock.format() == "Day 1 06:45"
        assert sender.events_of_type("state_update")[-1].payload["time_elapsed_minutes"] == 15
        await engine.shutdown()

    asyncio.run(run())


def test_oversized_estimate_is_clamped(tmp_path, monkeypatch):
    """Estimates beyond the configured cap are clamped instead of drifting."""

    async def run():
        engine, sender, _ = await started_timed_game(tmp_path, monkeypatch)

        async def resolve(
            actions, dice_results=None, hidden_rolls=None, current_time="", event_context=""
        ):
            return RoundResolution(
                global_narrative="Done.",
                player_resolutions={name: f"{name} done." for name in actions},
                time_elapsed_minutes=99999,
            )

        engine.resolver.generate_resolution = resolve
        await engine.process_payload("host", payload("action", action="Wait"))
        await engine.process_payload("player", payload("action", action="Wait too"))
        await engine.wait_for_inference()
        assert engine.game_clock.format() == "Day 1 08:30"
        assert sender.events_of_type("state_update")[-1].payload["time_elapsed_minutes"] == 120
        await engine.shutdown()

    asyncio.run(run())


def test_disabled_clock_sends_no_time_labels(tmp_path):
    """Without time_config, the clock stays off and no time labels are passed."""

    async def run():
        engine, sender, resolver = await build_started_game(tmp_path)
        assert not engine.time_enabled
        assert resolver.start_time == ""
        await engine.process_payload("host", payload("action", action="Opens the gate"))
        await engine.process_payload("player", payload("action", action="Keeps watch"))
        await engine.wait_for_inference()
        update = sender.events_of_type("state_update")[-1].payload
        assert update["game_time"] == ""
        assert update["time_elapsed_minutes"] is None
        await engine.shutdown()

    asyncio.run(run())


def test_time_state_survives_persistence_round_trip(tmp_path, monkeypatch):
    """The clock, config and rules are restored from a persisted room."""

    async def run():
        engine, sender, resolver = await started_timed_game(tmp_path, monkeypatch)
        await engine.process_payload("host", payload("action", action="Opens the gate"))
        await engine.process_payload("player", payload("action", action="Keeps watch"))
        await engine.wait_for_inference()
        restored = restore_engine(engine.to_persistent_dict(), sender, FakeResolver)
        assert restored.time_enabled
        assert restored.game_clock.format() == "Day 1 07:00"
        assert restored.max_elapsed_minutes == 120
        assert restored.default_elapsed_minutes == 15
        assert [rule.activity for rule in restored.time_rules] == ["短交流"]
        await restored.shutdown()

    asyncio.run(run())


def test_old_persisted_rooms_default_to_disabled_clock(tmp_path):
    """Rooms saved before time tracking existed restore without a clock."""

    async def run():
        sender = FakeSender()
        engine, _, _ = await build_started_game(tmp_path)
        await engine.shutdown()
        data = engine.to_persistent_dict()
        for key in ("time_enabled", "game_clock", "max_elapsed_minutes", "default_elapsed_minutes"):
            data.pop(key, None)
        data.pop("time_rules", None)
        restored = restore_engine(data, sender, FakeResolver)
        assert not restored.time_enabled
        assert restored.game_clock.format() == "Day 1 06:30"
        await restored.shutdown()

    asyncio.run(run())


def test_restored_clock_advances_again():
    """An advanced clock keeps accumulating after restore."""
    clock = GameClock.from_dict({"day": 4, "minute": 30})
    clock.add_minutes(90)
    assert clock.format() == "Day 4 02:00"
    assert GameClock.from_dict(None).format() == "Day 1 06:30"


def test_scenario_can_be_revised_before_start(tmp_path):
    """The host may re-submit scenario_init while the lobby is open."""

    async def run():
        sender = FakeSender()
        resolver = FakeResolver()
        engine = GameEngine(sender, resolver)
        engine.transcript = GameTranscript(tmp_path / "logs")
        await engine.host_join("host", {"name": "Host"})
        await engine.process_payload(
            "host",
            payload(
                "scenario_init",
                scenario="A gate.",
                characters=[{"name": "Host"}, {"name": "Old"}],
                host_character="Host",
                time_config={"enabled": True},
            ),
        )
        await engine.wait_for_inference()
        assert engine.state is GameState.AWAITING_PLAYERS
        await engine.player_join("p1", {"name": "One", "character": "Old"})
        assert engine.claims["Old"] == "One"
        await engine.process_payload(
            "host",
            payload(
                "scenario_init",
                scenario="A gate and a bridge.",
                characters=[{"name": "Host"}, {"name": "Old"}, {"name": "New"}],
                host_character="Host",
                time_config={"enabled": True},
            ),
        )
        await engine.wait_for_inference()
        assert engine.state is GameState.AWAITING_PLAYERS
        assert engine.original_scenario == "A gate and a bridge."
        # The surviving character keeps its claim; the host's own claim is reset.
        assert engine.claims["Old"] == "One"
        assert engine.players["p1"].character_name == "Old"
        await engine.shutdown()

    asyncio.run(run())


def test_revised_scenario_releases_removed_characters(tmp_path):
    """Claims on characters removed by an edit are released."""

    async def run():
        sender = FakeSender()
        resolver = FakeResolver()
        engine = GameEngine(sender, resolver)
        engine.transcript = GameTranscript(tmp_path / "logs")
        await engine.host_join("host", {"name": "Host"})
        await engine.process_payload(
            "host",
            payload(
                "scenario_init",
                scenario="A gate.",
                characters=[{"name": "Host"}, {"name": "Doomed"}],
                host_character="Host",
            ),
        )
        await engine.wait_for_inference()
        await engine.player_join("p1", {"name": "One", "character": "Doomed"})
        await engine.process_payload(
            "host",
            payload(
                "scenario_init",
                scenario="A gate.",
                characters=[{"name": "Host"}],
                host_character="Host",
            ),
        )
        await engine.wait_for_inference()
        assert "Doomed" not in engine.claims
        assert engine.players["p1"].character_name is None
        await engine.shutdown()

    asyncio.run(run())

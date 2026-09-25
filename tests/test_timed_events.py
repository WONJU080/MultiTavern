"""Fixed-time story events: scheduling, firing, injection, and persistence."""

import asyncio

import pytest

from logic.engine import GameEngine, restore_engine
from logic.lobby import parse_events
from logic.models import GameState
from logic.transcript import GameTranscript

from test_engine import FakeResolver, FakeSender, payload


async def started_game_with_events(tmp_path, events, **time_overrides):
    """Build a started timed game with the given scheduled events."""
    sender = FakeSender()
    resolver = FakeResolver()
    engine = GameEngine(sender, resolver)
    engine.transcript = GameTranscript(tmp_path / "logs")
    await engine.host_join("host", {"name": "Host"})
    time_config = {
        "enabled": True,
        "start_day": 1,
        "start_minute": 390,
        "max_elapsed_minutes": 120,
        "default_elapsed_minutes": 15,
        **time_overrides,
    }
    await engine.process_payload(
        "host",
        payload(
            "scenario_init",
            scenario="A gate blocks the road.",
            characters=[{"name": "Host"}, {"name": "Player"}],
            host_character="Host",
            time_config=time_config,
            events=events,
        ),
    )
    await engine.wait_for_inference()
    await engine.player_join("player", {"name": "Player", "character": "Player"})
    await engine.process_payload("host", payload("start_game"))
    await engine.wait_for_inference()
    return engine, sender, resolver


async def play_round(engine, host_action="Wait", player_action="Wait too"):
    """Submit both players' actions and wait for the round to commit."""
    await engine.process_payload("host", payload("action", action=host_action))
    await engine.process_payload("player", payload("action", action=player_action))
    await engine.wait_for_inference()


def test_parse_events_validation():
    """Event configuration is validated and unique by name."""
    events = parse_events(
        [
            {
                "name": "午时广播",
                "day": 1,
                "minute": 720,
                "description": "全城广播响起",
                "public": True,
            }
        ]
    )
    assert events[0].name == "午时广播" and events[0].public
    with pytest.raises(ValueError):
        parse_events(
            [
                {"name": "A", "day": 1, "minute": 0, "description": "x"},
                {"name": "a", "day": 1, "minute": 0, "description": "y"},
            ]
        )
    with pytest.raises(ValueError):
        parse_events([{"name": "A", "day": 1, "minute": 0, "description": "x", "public": "yes"}])
    with pytest.raises(ValueError):
        parse_events([{"name": "A", "day": 1, "minute": 0}])
    with pytest.raises(ValueError):
        parse_events([{"name": "A", "day": 1, "minute": 2000, "description": "x"}])


def test_events_without_clock_are_rejected(tmp_path):
    """Scheduling events requires the in-game clock to be enabled."""

    async def run():
        sender = FakeSender()
        engine = GameEngine(sender, FakeResolver())
        await engine.host_join("host", {"name": "Host"})
        await engine.process_payload(
            "host",
            payload(
                "scenario_init",
                scenario="A cabin.",
                characters=[{"name": "Host"}],
                host_character="Host",
                events=[{"name": "E", "day": 1, "minute": 0, "description": "x"}],
            ),
        )
        error = sender.events_of_type("error")[-1].payload["msg"]
        assert "require an enabled" in error
        assert engine.state is GameState.SCENARIO_INJECTION
        await engine.shutdown()

    asyncio.run(run())


def test_public_event_fires_when_the_clock_passes(tmp_path):
    """A due event fires once, announces itself, and is logged."""

    async def run():
        engine, sender, _ = await started_game_with_events(
            tmp_path,
            [
                {
                    "name": "午时广播",
                    "day": 1,
                    "minute": 420,
                    "description": "全城广播响起",
                    "public": True,
                }
            ],
        )
        await play_round(engine)
        assert engine.game_clock.format() == "Day 1 07:00"
        assert "午时广播" in engine.fired_events
        assert engine.event_log[0]["fired_at"] == "Day 1 07:00"
        assert engine.event_log[0]["public"] is True
        announcements = [
            event.payload["msg"]
            for event in sender.events_of_type("system_msg")
            if "全局事件" in event.payload["msg"]
        ]
        assert announcements == ["全局事件「午时广播」: 全城广播响起"]
        # The injection waits for the next round and is not duplicated on refire.
        await play_round(engine)
        assert engine.game_clock.format() == "Day 1 07:30"
        assert engine.event_log[0]["fired_total_minutes"] == 1440 + 420
        assert len(engine.event_log) == 1
        assert engine.pending_event_injections == []
        await engine.shutdown()

    asyncio.run(run())


def test_event_does_not_fire_before_its_time(tmp_path):
    """A future event stays dormant until the clock reaches it."""

    async def run():
        engine, sender, _ = await started_game_with_events(
            tmp_path,
            [
                {
                    "name": "深夜异响",
                    "day": 1,
                    "minute": 600,
                    "description": "远处传来怪声",
                    "public": False,
                }
            ],
        )
        await play_round(engine)
        assert engine.game_clock.format() == "Day 1 07:00"
        assert engine.fired_events == set()
        assert engine.event_log == []
        announcements = [
            event.payload["msg"]
            for event in sender.events_of_type("system_msg")
            if "全局事件" in event.payload["msg"]
        ]
        assert announcements == []
        await engine.shutdown()

    asyncio.run(run())


def test_fired_event_injection_reaches_the_next_resolution(tmp_path):
    """The fired event is injected into the following round's request."""

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
        await play_round(engine)
        assert resolver.event_context == ""
        await play_round(engine)
        assert "Scheduled event fired at Day 1 07:00" in resolver.event_context
        assert "午时广播" in resolver.event_context
        await engine.shutdown()

    asyncio.run(run())


def test_backstage_event_has_no_public_announcement(tmp_path):
    """Non-public events only feed the narrative, never the chat."""

    async def run():
        engine, sender, _ = await started_game_with_events(
            tmp_path,
            [
                {
                    "name": "暗中监视",
                    "day": 1,
                    "minute": 420,
                    "description": "有人跟踪玩家",
                    "public": False,
                }
            ],
        )
        await play_round(engine)
        assert "暗中监视" in engine.fired_events
        assert engine.event_log[0]["public"] is False
        announcements = [
            event.payload["msg"]
            for event in sender.events_of_type("system_msg")
            if "全局事件" in event.payload["msg"]
        ]
        assert announcements == []
        await engine.shutdown()

    asyncio.run(run())


def test_events_survive_persistence(tmp_path):
    """Schedules, fired status and the event log are restored."""

    async def run():
        engine, sender, _ = await started_game_with_events(
            tmp_path,
            [
                {
                    "name": "午时广播",
                    "day": 1,
                    "minute": 420,
                    "description": "全城广播响起",
                    "public": True,
                }
            ],
        )
        await play_round(engine)
        restored = restore_engine(engine.to_persistent_dict(), sender, FakeResolver)
        assert [event.name for event in restored.timed_events] == ["午时广播"]
        assert restored.fired_events == {"午时广播"}
        assert restored.event_log[0]["fired_at"] == "Day 1 07:00"
        assert restored.game_clock.format() == "Day 1 07:00"
        await restored.shutdown()

    asyncio.run(run())

"""Strict (per-action) scheduling: staggered completions, unification, hold, disconnect."""

import asyncio
from pathlib import Path

from core.schemas import ActionReview
from logic.engine import GameEngine, GameState
from logic.transcript import GameTranscript
from test_engine import FakeResolver, FakeSender, payload

TIME_CONFIG = {
    "enabled": True,
    "start_day": 1,
    "start_minute": 480,
    "max_elapsed_minutes": 600,
    "default_elapsed_minutes": 30,
}


async def build_strict(tmp_path: Path) -> tuple[GameEngine, FakeSender, FakeResolver]:
    """Start a three-character game in strict scheduling mode."""
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
            characters=[{"name": "Host"}, {"name": "Player"}, {"name": "Scout"}],
            host_character="Host",
            time_config=TIME_CONFIG,
            schedule="strict",
            random_turn_order=False,
        ),
    )
    await engine.wait_for_inference()
    await engine.player_join("p1", {"name": "Player", "character": "Player"})
    await engine.player_join("p2", {"name": "Scout", "character": "Scout"})
    await engine.process_payload("host", payload("start_game"))
    await engine.wait_for_inference()
    return engine, sender, resolver


def window_event(sender: FakeSender) -> dict:
    return sender.events_of_type("turn_window")[-1].payload


def test_strict_staggers_completions(tmp_path: Path) -> None:
    """A fast action completes and can act again while a slow one is still running."""

    async def run() -> None:
        engine, sender, resolver = await build_strict(tmp_path)
        assert engine.state is GameState.ACTIVE_TURN
        assert set(window_event(sender)["active_player_ids"]) == {"host", "p1", "p2"}
        assert engine.game_clock.format() == "Day 1 08:00"

        resolver.review_results = [ActionReview(valid=True, duration_minutes=60)]
        await engine.process_payload("host", payload("action", action="擦枪"))
        resolver.review_results = [ActionReview(valid=True, duration_minutes=30)]
        await engine.process_payload("p1", payload("action", action="读信"))
        resolver.review_results = [ActionReview(valid=True, duration_minutes=30)]
        await engine.process_payload("p2", payload("action", action="包扎"))
        await engine.wait_for_inference()

        # Host (60) is still busy; the two 30-minute actions completed at 08:30.
        assert engine.inflight.get("host", {}).get("duration") == 60
        assert "p1" not in engine.inflight and "p2" not in engine.inflight
        assert engine.game_clock.format() == "Day 1 08:30"
        assert set(engine.window) == {"p1", "p2"}
        waiting = window_event(sender)["waiting"]
        assert any(item["name"] == "Host" for item in waiting)

        await engine.process_payload("p1", payload("action", action="观察"))
        assert engine.state is GameState.ACTIVE_TURN

    asyncio.run(run())


def test_nearly_equal_durations_unify_to_the_longest(tmp_path: Path) -> None:
    """Actions within 15 minutes of each other finish together at the maximum."""

    async def run() -> None:
        engine, sender, resolver = await build_strict(tmp_path)
        for cid, duration, text in (("host", 60, "挖"), ("p1", 50, "搬"), ("p2", 45, "扛")):
            resolver.review_results = [ActionReview(valid=True, duration_minutes=duration)]
            await engine.process_payload(cid, payload("action", action=text))
        await engine.wait_for_inference()
        assert engine.inflight == {}
        assert engine.game_clock.format() == "Day 1 09:00"
        assert set(engine.window) == {"host", "p1", "p2"}

    asyncio.run(run())


def test_hold_releases_the_window_without_spending_time(tmp_path: Path) -> None:
    """Holding lets a free player wait while others act."""

    async def run() -> None:
        engine, _, resolver = await build_strict(tmp_path)
        resolver.review_results = [ActionReview(valid=True, duration_minutes=60)]
        await engine.process_payload("host", payload("action", action="擦枪"))
        resolver.review_results = [ActionReview(valid=True, duration_minutes=30)]
        await engine.process_payload("p1", payload("action", action="读信"))
        await engine.process_payload("p2", payload("hold"))

        await engine.wait_for_inference()
        # p2 held; once the 30-minute actions finish, p1 is free again and p2 is free too.
        assert engine.game_clock.format() == "Day 1 08:30"
        assert "host" in engine.inflight
        assert engine.holds == set()

    asyncio.run(run())


def test_disconnect_does_not_block_the_timeline(tmp_path: Path) -> None:
    """A disconnected idle player is dropped from the window; an acting one still finishes."""

    async def run() -> None:
        engine, _, resolver = await build_strict(tmp_path)
        resolver.review_results = [ActionReview(valid=True, duration_minutes=60)]
        await engine.process_payload("host", payload("action", action="挖"))
        await engine.handle_disconnect("p1")
        await engine.handle_disconnect("p2")
        await engine.wait_for_inference()
        assert engine.inflight == {}
        assert engine.game_clock.format() == "Day 1 09:00"
        assert set(engine.window) == {"host"}

    asyncio.run(run())


def test_retry_paused_strict_boundary(tmp_path: Path) -> None:
    """A failed strict resolution pauses and can be retried by the host."""

    async def run() -> None:
        engine, _, resolver = await build_strict(tmp_path)
        attempts = {"n": 0}
        original = resolver.generate_resolution

        async def flaky(round_buffer, *args, **kwargs):
            attempts["n"] += 1
            if attempts["n"] == 1:
                raise RuntimeError("boom")
            return await original(round_buffer, *args, **kwargs)

        resolver.generate_resolution = flaky
        resolver.review_results = [ActionReview(valid=True, duration_minutes=30)]
        await engine.process_payload("host", payload("action", action="挖"))
        resolver.review_results = [ActionReview(valid=True, duration_minutes=30)]
        await engine.process_payload("p1", payload("action", action="搬"))
        resolver.review_results = [ActionReview(valid=True, duration_minutes=30)]
        await engine.process_payload("p2", payload("action", action="扛"))
        await engine.wait_for_inference()
        assert engine.round_paused is True

        await engine.process_payload("host", payload("retry_round"))
        await engine.wait_for_inference()
        assert engine.round_paused is False
        assert engine.inflight == {}
        assert engine.game_clock.format() == "Day 1 08:30"

    asyncio.run(run())


def test_strict_skip_vote_removes_an_idle_player(tmp_path: Path) -> None:
    """A unanimous vote removes an idle windowed player in strict mode."""

    async def run() -> None:
        engine, _, _ = await build_strict(tmp_path)
        await engine.process_payload("host", payload("skip_vote", target="p1"))
        assert "p1" in engine.players
        await engine.process_payload("p2", payload("skip_vote", target="p1"))
        assert "p1" not in engine.players
        assert "p1" not in engine.join_order
        assert "p1" not in engine.window

    asyncio.run(run())


def test_strict_skip_vote_requires_unanimity(tmp_path: Path) -> None:
    """A single vote does not remove the target."""

    async def run() -> None:
        engine, _, _ = await build_strict(tmp_path)
        await engine.process_payload("host", payload("skip_vote", target="p1"))
        assert "p1" in engine.players

    asyncio.run(run())


def test_strict_skip_vote_rejects_an_inflight_target(tmp_path: Path) -> None:
    """A player who is currently acting cannot be skipped."""

    async def run() -> None:
        engine, sender, resolver = await build_strict(tmp_path)
        resolver.review_results = [ActionReview(valid=True, duration_minutes=60)]
        await engine.process_payload("host", payload("action", action="挖"))
        await engine.process_payload("p1", payload("skip_vote", target="host"))
        assert sender.events_of_type("error")[-1].payload["msg"] == "目标正在行动中，无法跳过。"

    asyncio.run(run())


def test_rejected_action_reopens_the_strict_window(tmp_path: Path) -> None:
    """A rejected strict action re-sends the window so the player can retry."""

    async def run() -> None:
        engine, sender, resolver = await build_strict(tmp_path)
        resolver.review_results = [ActionReview(valid=False, reason="请具体一点。")]
        await engine.process_payload("host", payload("action", action="做出正确的决定"))
        assert sender.events_of_type("error")[-1].payload["msg"] == "请具体一点。"
        assert "host" in engine.window
        personal = [
            event
            for client_id, event in sender.events
            if client_id == "host" and event.type == "turn_window"
        ]
        assert personal
        assert "host" in personal[-1].payload["active_player_ids"]

    asyncio.run(run())


def test_strict_records_history_and_transcript(tmp_path: Path) -> None:
    """Strict completions are written to the round history and HTML transcript."""

    async def run() -> None:
        engine, _, resolver = await build_strict(tmp_path)
        for cid, text in (("host", "挖"), ("p1", "搬"), ("p2", "扛")):
            resolver.review_results = [ActionReview(valid=True, duration_minutes=30)]
            await engine.process_payload(cid, payload("action", action=text))
        await engine.wait_for_inference()
        assert len(engine.round_history) == 1
        record = engine.round_history[0]
        assert record["round_number"] == 1
        assert record["actions"] == {"Host": "挖", "Player": "搬", "Scout": "扛"}
        assert engine.transcript.path is not None
        content = engine.transcript.path.read_text(encoding="utf-8")
        assert "<h2>Round 1</h2>" in content

    asyncio.run(run())



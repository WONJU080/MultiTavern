"""Action review rejects vague outcome claims and asks the player to re-input."""

import asyncio
from pathlib import Path

from core.schemas import ActionReview
from logic.engine import GameEngine
from logic.transcript import GameTranscript
from test_engine import FakeResolver, FakeSender, payload


async def build_game(tmp_path: Path) -> tuple[GameEngine, FakeSender, FakeResolver]:
    """Start a deterministic two-character game for action review tests."""
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
            random_turn_order=False,
        ),
    )
    await engine.wait_for_inference()
    await engine.player_join("player", {"name": "Player", "character": "Player"})
    await engine.process_payload("host", payload("start_game"))
    await engine.wait_for_inference()
    return engine, sender, resolver


def test_vague_action_is_rejected_and_player_can_retry(tmp_path: Path) -> None:
    """A vague outcome claim is rejected and the player's turn is restored."""

    async def run() -> None:
        engine, sender, resolver = await build_game(tmp_path)
        assert engine.active_player_id == "host"
        resolver.review_results = [ActionReview(valid=False, reason="请说明角色具体做了什么。")]

        await engine.process_payload("host", payload("action", action="做出了正确的决定"))
        assert sender.events_of_type("error")[-1].payload["msg"] == "请说明角色具体做了什么。"
        assert engine.round_buffer == {}
        directives = [
            event
            for client_id, event in sender.events
            if client_id == "host" and event.type == "turn_directive"
        ]
        assert directives[-1].payload["active_player_id"] == "host"

        await engine.process_payload("host", payload("action", action="翻开账本核对日期"))
        assert engine.round_buffer == {"host": "翻开账本核对日期"}
        assert sender.events_of_type("action_echo")[-1].payload["action"] == "翻开账本核对日期"
        assert engine.active_player_id == "player"
        assert resolver.action_reviews == [
            ("Host", "做出了正确的决定"),
            ("Host", "翻开账本核对日期"),
        ]

    asyncio.run(run())


def test_action_review_backend_failure_accepts_action(tmp_path: Path) -> None:
    """A review outage fails open so play continues."""

    async def run() -> None:
        engine, _, resolver = await build_game(tmp_path)
        resolver.review_results = [RuntimeError("backend down")]
        await engine.process_payload("host", payload("action", action="守在门口观察来往的人"))
        assert engine.round_buffer == {"host": "守在门口观察来往的人"}

    asyncio.run(run())

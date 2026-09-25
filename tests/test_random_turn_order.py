"""Randomized per-round action input order."""

import asyncio
import random

from logic.engine import GameEngine, restore_engine
from logic.transcript import GameTranscript

from test_engine import FakeResolver, FakeSender, build_started_game, payload


def test_default_is_random_and_toggle_parses(tmp_path):
    """The toggle defaults on and can be switched off via scenario_init."""

    async def run():
        sender = FakeSender()
        resolver = FakeResolver()
        engine = GameEngine(sender, resolver)
        engine.transcript = GameTranscript(tmp_path / "logs")
        assert engine.random_turn_order
        await engine.host_join("host", {"name": "Host"})
        await engine.process_payload(
            "host",
            payload(
                "scenario_init",
                scenario="A gate.",
                characters=[{"name": "Host"}],
                host_character="Host",
                random_turn_order=False,
            ),
        )
        await engine.wait_for_inference()
        assert not engine.random_turn_order
        await engine.process_payload(
            "host",
            payload(
                "scenario_init",
                scenario="A gate.",
                characters=[{"name": "Host"}],
                host_character="Host",
                random_turn_order="yes",
            ),
        )
        error = sender.events_of_type("error")[-1].payload["msg"]
        assert "must be a boolean" in error
        await engine.shutdown()

    asyncio.run(run())


def test_shuffle_runs_at_start_and_after_every_round(tmp_path, monkeypatch):
    """The queue is shuffled once at game start and once per round commit."""

    async def run():
        calls = []

        def spy(sequence):
            calls.append(list(sequence))

        monkeypatch.setattr(random, "shuffle", spy)
        engine, _, _ = await build_started_game(tmp_path)
        assert len(calls) == 1  # start shuffle
        await engine.process_payload("host", payload("action", action="Opens the gate"))
        await engine.process_payload("player", payload("action", action="Keeps watch"))
        await engine.wait_for_inference()
        assert len(calls) == 2  # commit shuffle
        assert calls[0] == ["host", "player"]
        assert calls[1] == ["host", "player"]
        await engine.shutdown()

    asyncio.run(run())


def test_reversed_order_round_completes_correctly(tmp_path, monkeypatch):
    """With a forced permutation, the round still resolves in that order."""

    async def run():
        monkeypatch.setattr(random, "shuffle", lambda sequence: sequence.reverse())
        engine, sender, _ = await build_started_game(tmp_path)
        # Forced reverse makes the player act first.
        assert engine.active_player_id == "player"
        await engine.process_payload("player", payload("action", action="Keeps watch"))
        assert engine.active_player_id == "host"
        await engine.process_payload("host", payload("action", action="Opens the gate"))
        await engine.wait_for_inference()
        assert engine.round_counter == 1
        state = sender.events_of_type("state_update")[-1].payload
        assert set(state["player_resolutions"]) == {"Host", "Player"}
        await engine.shutdown()

    asyncio.run(run())


def test_toggle_off_keeps_join_order(tmp_path, monkeypatch):
    """With randomization disabled, the queue is never shuffled."""

    async def run():
        calls = []

        def spy(sequence):
            calls.append(list(sequence))

        monkeypatch.setattr(random, "shuffle", spy)
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
                characters=[{"name": "Host"}, {"name": "Player"}],
                host_character="Host",
                random_turn_order=False,
            ),
        )
        await engine.wait_for_inference()
        await engine.player_join("player", {"name": "Player", "character": "Player"})
        await engine.process_payload("host", payload("start_game"))
        await engine.wait_for_inference()
        assert calls == []
        assert engine.active_player_id == "host"
        await engine.process_payload("host", payload("action", action="Opens the gate"))
        await engine.process_payload("player", payload("action", action="Keeps watch"))
        await engine.wait_for_inference()
        assert calls == []
        await engine.shutdown()

    asyncio.run(run())


def test_disconnected_and_skip_flow_survive_shuffling(tmp_path, monkeypatch):
    """Disconnect-idle injection works inside a shuffled queue."""

    async def run():
        monkeypatch.setattr(random, "shuffle", lambda sequence: sequence.reverse())
        engine, sender, _ = await build_started_game(tmp_path)
        await engine.handle_disconnect("player")
        # Player's turn comes first in the reversed order; the host still acts.
        await engine.process_payload("host", payload("action", action="Opens the gate"))
        await engine.wait_for_inference()
        assert engine.round_counter == 1
        state = sender.events_of_type("state_update")[-1].payload
        assert "Player" in state["player_resolutions"]
        await engine.shutdown()

    asyncio.run(run())


def test_toggle_and_order_survive_persistence(tmp_path):
    """The toggle persists and a restored room keeps a fresh queue."""

    async def run():
        sender = FakeSender()
        engine, _, _ = await build_started_game(tmp_path)
        assert engine.random_turn_order
        restored = restore_engine(engine.to_persistent_dict(), sender, FakeResolver)
        assert restored.random_turn_order
        assert list(restored.turn_queue) == list(restored.join_order)
        await engine.shutdown()
        await restored.shutdown()

    asyncio.run(run())

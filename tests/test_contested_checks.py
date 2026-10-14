"""Contested-action checks: the referee's ``targets`` drives a server-side guarantee.

The referee already classifies which characters an action targets. These tests pin
that classification to the dice-planning guard, so a player-versus-player contest
cannot silently resolve with no check at all just because the planner said false.
"""

import asyncio
from pathlib import Path

import pytest

from core.schemas import ActionReview, DicePlan
from logic.engine import GameEngine, GameState
from logic.llm_manager import LLMResolutionError
from logic.transcript import GameTranscript

from test_engine import FakeResolver, FakeSender, payload


def reviews(**entries) -> dict[str, dict]:
    """Build a referee-metadata snapshot keyed by character name."""
    return {name: {"kind": value[0], "targets": list(value[1])} for name, value in entries.items()}


def test_mutual_targets_are_detected():
    """Two characters naming each other form one opposed pair."""
    snapshot = reviews(
        金元珠=("target_other", ["Ram"]),
        Ram=("target_other", ["金元珠"]),
    )
    pairs = GameEngine._mutually_opposed(snapshot)
    assert sorted(tuple(sorted(pair)) for pair in pairs) == [tuple(sorted(("金元珠", "Ram")))]


def test_one_sided_targeting_is_not_a_contest():
    """A single actor targeting someone who is not retaliating is not opposed."""
    snapshot = reviews(
        金元珠=("target_other", ["Ram"]),
        Ram=("self", []),
    )
    assert GameEngine._mutually_opposed(snapshot) == []


def test_targeting_an_absent_character_is_ignored():
    """A target outside this resolution is not part of the contest."""
    snapshot = reviews(金元珠=("target_other", ["Ram"]))
    assert GameEngine._mutually_opposed(snapshot) == []


def test_pair_is_reported_once():
    """The same opposed pair is never reported twice."""
    snapshot = reviews(
        金元珠=("target_other", ["Ram"]),
        Ram=("target_other", ["金元珠"]),
    )
    assert len(GameEngine._mutually_opposed(snapshot)) == 1


def test_unchecked_contest_is_rejected():
    """A plan that leaves an opposed pair unchecked raises instead of resolving."""
    snapshot = reviews(
        金元珠=("target_other", ["Ram"]),
        Ram=("target_other", ["金元珠"]),
    )
    with pytest.raises(LLMResolutionError, match="对抗行动缺少检定"):
        GameEngine._require_contested_checks(snapshot, set())


def test_half_checked_contest_is_rejected():
    """Checking only one side of an opposed pair is not enough."""
    snapshot = reviews(
        金元珠=("target_other", ["Ram"]),
        Ram=("target_other", ["金元珠"]),
    )
    with pytest.raises(LLMResolutionError, match="对抗行动缺少检定"):
        GameEngine._require_contested_checks(snapshot, {"金元珠"})


def test_fully_checked_contest_passes():
    """Both sides checked satisfies the guarantee."""
    snapshot = reviews(
        金元珠=("target_other", ["Ram"]),
        Ram=("target_other", ["金元珠"]),
    )
    GameEngine._require_contested_checks(snapshot, {"金元珠", "Ram"})


def test_no_contest_requires_no_checks():
    """A round with no mutual targeting may legitimately roll nothing."""
    snapshot = reviews(Host=("self", []), Player=("self", []))
    GameEngine._require_contested_checks(snapshot, set())


async def build_started_game(tmp_path: Path) -> tuple[GameEngine, FakeSender, FakeResolver]:
    """Start a two-character game with an all-check plan available."""
    sender = FakeSender()
    resolver = FakeResolver()
    engine = GameEngine(sender, resolver)
    engine.transcript = GameTranscript(tmp_path / "logs")
    await engine.host_join("host", {"name": "Host"})
    await engine.process_payload(
        "host",
        payload(
            "scenario_init",
            scenario="A cabin.",
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


def test_round_freezes_review_targets_for_the_guard(tmp_path: Path) -> None:
    """A real round snapshots the referee targets and clears them on consumption."""

    async def run() -> None:
        engine, _, resolver = await build_started_game(tmp_path)
        resolver.review_results = [
            ActionReview(valid=True, kind="target_other", targets=["Player"]),
            ActionReview(valid=True, kind="target_other", targets=["Host"]),
        ]
        await engine.process_payload("host", payload("action", action="扑向Player"))
        # The first action's review is recorded while the round is still collecting.
        assert engine.action_reviews["Host"]["targets"] == ["Player"]

        async def unchecked(actions, current_state=""):
            return DicePlan(rolls={name: False for name in actions}, hidden_rolls=[])

        resolver.plan_dice = unchecked
        await engine.process_payload("player", payload("action", action="反击Host"))
        await engine.wait_for_inference()

        # The guard refused the unchecked contest: the round never committed.
        assert engine.round_counter == 0
        assert engine.round_paused is True
        assert engine.state is GameState.AWAITING_LLM
        frozen = engine.pending_resolution["action_reviews"]
        assert frozen["Host"]["targets"] == ["Player"]
        assert frozen["Player"]["targets"] == ["Host"]
        await engine.shutdown()

    asyncio.run(run())


def test_reviews_are_consumed_by_one_round(tmp_path: Path) -> None:
    """Referee targets do not leak into a later round."""

    async def run() -> None:
        engine, _, resolver = await build_started_game(tmp_path)
        # FakeResolver checks every action by default, so the pair is satisfied.
        resolver.review_results = [
            ActionReview(valid=True, kind="target_other", targets=["Player"]),
            ActionReview(valid=True, kind="target_other", targets=["Host"]),
        ]
        await engine.process_payload("host", payload("action", action="扑向Player"))
        await engine.process_payload("player", payload("action", action="反击Host"))
        await engine.wait_for_inference()
        assert engine.round_counter == 1
        # Consumed by the resolved round.
        assert engine.action_reviews == {}
        await engine.shutdown()

    asyncio.run(run())


def test_kind_must_match_its_structured_field(tmp_path: Path) -> None:
    """A declared proposal/response without its target is rejected, not dropped."""

    async def run() -> None:
        engine, sender, resolver = await build_started_game(tmp_path)
        resolver.review_results = [ActionReview(valid=True, kind="proposal", proposal_to=None)]
        await engine.process_payload("host", payload("action", action="我们一起搜查吧"))
        assert "请写明向哪位队友提议" in sender.events_of_type("error")[-1].payload["msg"]
        assert engine.round_buffer == {}

        resolver.review_results = [ActionReview(valid=True, kind="response", response_to=None)]
        await engine.process_payload("host", payload("action", action="我同意"))
        assert "请写明回应的是哪位角色" in sender.events_of_type("error")[-1].payload["msg"]
        await engine.shutdown()

    asyncio.run(run())

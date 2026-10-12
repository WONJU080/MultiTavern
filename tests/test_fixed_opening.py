"""A host-provided fixed opening is shown verbatim before the first round."""

import asyncio
from pathlib import Path

from logic.engine import GameEngine, GameState, restore_engine
from logic.transcript import GameTranscript
from test_engine import FakeResolver, FakeSender, payload

OPENING = (
    "雨夜，三人在旧档案室碰面。\n\n"
    "此刻——角色甲在校对现场记录；角色乙靠着窗听码头动静；角色丙摊开一摞旧报纸。"
)


async def build_game(
    tmp_path: Path, opening: str = OPENING
) -> tuple[GameEngine, FakeSender, FakeResolver]:
    """Start a deterministic game with an optional fixed opening."""
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
            opening=opening,
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


def test_fixed_opening_is_used_verbatim_without_generation(tmp_path: Path) -> None:
    """A fixed opening is broadcast as-is and the resolver skips start generation."""

    async def run() -> None:
        engine, sender, resolver = await build_game(tmp_path)
        assert engine.state is GameState.ACTIVE_TURN
        assert engine.opening_scenario == OPENING
        assert engine.current_scenario_state == OPENING
        assert resolver.start_names == []
        assert resolver.fixed_opening == OPENING
        update = sender.events_of_type("state_update")[-1].payload
        assert update["global_narrative"] == OPENING

    asyncio.run(run())


def test_absent_opening_falls_back_to_generated_start(tmp_path: Path) -> None:
    """Without a fixed opening the engine still generates the opening."""

    async def run() -> None:
        engine, _, resolver = await build_game(tmp_path, opening="")
        assert resolver.start_names == [["Host", "Player"]]
        assert engine.opening_scenario == "Host, Player stand at a gate."

    asyncio.run(run())


def test_fixed_opening_survives_restore(tmp_path: Path) -> None:
    """The fixed opening and its resolver context are rebuilt after a restart."""

    async def run() -> None:
        engine, _, _ = await build_game(tmp_path)
        data = engine.to_persistent_dict()
        assert data["fixed_opening"] == OPENING
        restored = restore_engine(data, FakeSender(), FakeResolver)
        assert restored.fixed_opening == OPENING
        assert restored.opening_scenario == OPENING
        assert restored.resolver.fixed_opening == OPENING

    asyncio.run(run())

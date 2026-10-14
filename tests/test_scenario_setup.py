"""Shared scenario validation: the server and the offline tool must agree.

``parse_scenario_setup`` is the single implementation behind both the
``scenario_init`` handler and ``playtest validate``. If these drift apart, a card
can pass the offline check and then be rejected on a live server, so the parity
test below asserts both paths produce the same message.
"""

import asyncio
from pathlib import Path

import pytest

from logic.engine import GameEngine, GameState
from logic.lobby import ScenarioSetup, parse_scenario_setup
from logic.transcript import GameTranscript
from test_engine import FakeResolver, FakeSender, payload

BASE: dict[str, object] = {
    "scenario": "A gate blocks the road.",
    "characters": [{"name": "Host", "description": "Keeper"}, {"name": "Guest"}],
    "host_character": "Host",
}

REGIONS: list[dict[str, object]] = [
    {"id": "hall", "name": "Hall", "items": ["key"], "links": [{"to": "study"}]},
    {"id": "study", "name": "Study", "items": [], "links": []},
]
ITEMS: list[dict[str, object]] = [{"id": "key", "name": "Brass key", "region": "hall"}]

# Each entry pairs a payload the server must reject with the message it must use.
INVALID_PAYLOADS: list[tuple[dict[str, object], str]] = [
    ({"scenario": "A gate."}, "'characters' 必须是非空列表"),
    ({"scenario": "A gate.", "characters": []}, "'characters' 必须是非空列表"),
    ({"characters": [{"name": "Host"}]}, "'scenario' 必须是字符串"),
    ({**BASE, "random_turn_order": "yes"}, "'random_turn_order' 必须是布尔值"),
    ({**BASE, "schedule": "timeline"}, "'schedule' 必须是 'round' 或 'strict'"),
    (
        {**BASE, "regions": REGIONS, "items": ITEMS, "schedule": "round"},
        "地区系统需要 schedule='strict'。",
    ),
    ({**BASE, "schedule": "strict"}, "严格时间轴调度需要先启用游戏内时钟。"),
    (
        {
            **BASE,
            "events": [{"name": "Knock", "description": "Someone knocks.", "minute": 100}],
        },
        "定时事件需要先启用游戏内时钟。",
    ),
    ({**BASE, "world": {"start_region": "hall"}}, "'world' 需要同时提供 'regions'。"),
    (
        {**BASE, "regions": REGIONS, "items": ITEMS, "world": {"start_region": "attic"}},
        "world.start_region 不在 regions 中。",
    ),
    (
        {
            **BASE,
            "regions": [{**REGIONS[0], "items": ["ghost"]}, REGIONS[1]],
            "items": ITEMS,
        },
        "地区 'hall' 引用了不存在的物品 'ghost'。",
    ),
    (
        {
            **BASE,
            "regions": [{**REGIONS[0], "links": [{"to": "void"}]}, REGIONS[1]],
            "items": ITEMS,
        },
        "地区 'hall' 连接指向不存在的地区 'void'。",
    ),
    (
        {
            **BASE,
            "regions": [
                {**REGIONS[0], "links": [{"to": "study", "requires_item": "ghost"}]},
                REGIONS[1],
            ],
            "items": ITEMS,
        },
        "地区 'hall' 的连接要求不存在的物品。",
    ),
    (
        {
            **BASE,
            "time_config": {"enabled": True},
            "regions": REGIONS,
            "items": ITEMS,
            "events": [
                {
                    "name": "Knock",
                    "description": "Someone knocks.",
                    "region": "void",
                    "when": "enter",
                }
            ],
        },
        "事件 'Knock' 引用了不存在的地区 'void'。",
    ),
    ({**BASE, "host_character": "Stranger"}, "房主选择的角色不在角色名单中。"),
]


def test_parse_scenario_setup_normalizes_a_minimal_payload() -> None:
    """A region-less card defaults to round scheduling and keeps the cast order."""
    setup = parse_scenario_setup(dict(BASE))

    assert isinstance(setup, ScenarioSetup)
    assert [character.name for character in setup.cast] == ["Host", "Guest"]
    assert setup.host_character == "Host"
    assert setup.schedule == "round"
    assert setup.start_region == ""
    assert setup.regions == []
    assert setup.random_turn_order is True
    assert setup.sampling.temperature is None


def test_parse_scenario_setup_derives_strict_mode_and_start_region() -> None:
    """A card with regions needs a clock and defaults to strict, first region first."""
    setup = parse_scenario_setup(
        {**BASE, "regions": REGIONS, "items": ITEMS, "time_config": {"enabled": True}}
    )

    assert setup.schedule == "strict"
    assert setup.start_region == "hall"
    assert [region.id for region in setup.regions] == ["hall", "study"]
    assert setup.time_config.enabled is True


def test_parse_scenario_setup_canonicalizes_the_host_claim() -> None:
    """The host's claim resolves to the canonical cast spelling."""
    setup = parse_scenario_setup({**BASE, "host_character": "guest"})

    assert setup.host_character == "Guest"


@pytest.mark.parametrize("data, message", INVALID_PAYLOADS)
def test_shared_validator_rejects_with_the_expected_message(
    data: dict[str, object], message: str
) -> None:
    """Every rejection keeps its original wording."""
    with pytest.raises(ValueError) as excinfo:
        parse_scenario_setup(data)

    assert message in str(excinfo.value)


@pytest.mark.parametrize("data, message", INVALID_PAYLOADS)
def test_server_rejects_with_the_same_message_as_the_shared_validator(
    tmp_path: Path, data: dict[str, object], message: str
) -> None:
    """The engine's scenario_init path reports exactly what the tool reports."""

    async def run() -> tuple[str, GameState, int]:
        sender = FakeSender()
        engine = GameEngine(sender, FakeResolver())
        engine.transcript = GameTranscript(tmp_path / "logs")
        await engine.host_join("host", {"name": "Host"})
        await engine.process_payload("host", payload("scenario_init", **data))
        return (
            sender.events_of_type("error")[-1].payload["msg"],
            engine.state,
            len(engine.cast),
        )

    reported, state, cast_size = asyncio.run(run())

    with pytest.raises(ValueError) as excinfo:
        parse_scenario_setup(data)
    assert reported == str(excinfo.value)
    assert message in reported
    # A rejected payload never partially applies to the room.
    assert state is GameState.SCENARIO_INJECTION
    assert cast_size == 0


def test_the_engine_keeps_its_own_copy_of_the_validated_setup(tmp_path: Path) -> None:
    """Engine state must not alias the frozen setup returned to the caller."""

    async def run() -> ScenarioSetup:
        sender = FakeSender()
        engine = GameEngine(sender, FakeResolver())
        engine.transcript = GameTranscript(tmp_path / "logs")
        await engine.host_join("host", {"name": "Host"})
        data = {**BASE, "regions": REGIONS, "items": ITEMS, "time_config": {"enabled": True}}
        setup = parse_scenario_setup(data)
        await engine.process_payload("host", payload("scenario_init", **data))
        engine.cast.clear()
        engine.regions.clear()
        engine.lorebook.clear()
        return setup

    setup = asyncio.run(run())

    assert [character.name for character in setup.cast] == ["Host", "Guest"]
    assert [region.id for region in setup.regions] == ["hall", "study"]
    assert setup.schedule == "strict"

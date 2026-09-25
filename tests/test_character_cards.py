"""Character cards: parsing, fixed context, player edits, and persistence."""

import asyncio

import pytest

from core.config import settings
from core.schemas import Character, RoundResolution
from logic.engine import GameEngine, restore_engine
from logic.llm_manager import LLMContextManager
from logic.lobby import parse_cast
from logic.transcript import GameTranscript

from test_engine import FakeResolver, FakeSender, payload
from test_priority_one_llm import FakeClient


def test_parse_cast_accepts_card_fields():
    """Cast entries carry personality, style and example dialogue."""
    cast = parse_cast(
        [
            {
                "name": "郑宝和",
                "description": "自由港私掠船长",
                "personality": "关疏-值我-欲支-动突-表明-防透-压反",
                "style": "短句，嘴不留情",
                "example_dialogue": "郑家的人，轮不到外人收拾。",
            },
            {"name": "Host"},
        ]
    )
    assert cast[0].personality == "关疏-值我-欲支-动突-表明-防透-压反"
    assert cast[0].style == "短句，嘴不留情"
    assert cast[0].example_dialogue == "郑家的人，轮不到外人收拾。"
    assert cast[1].personality == ""
    with pytest.raises(ValueError):
        parse_cast([{"name": "A", "personality": 123}])
    with pytest.raises(ValueError):
        parse_cast([{"name": "A", "style": "x" * 10_001}])


def test_cast_cards_land_in_the_fixed_context():
    """The cast block follows genesis and updates via set_cast."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    client = FakeClient()

    async def run():
        manager = LLMContextManager(client)
        manager.set_genesis("A village in winter.")
        manager.set_cast([Character(name="Alice", personality="冷静", style="短句")])
        await manager.generate_resolution({"Alice": "Wait"})
        messages = client.calls[-1]["messages"]
        contents = [message["content"] for message in messages]
        genesis_index = next(i for i, c in enumerate(contents) if "A village in winter" in c)
        cast_index = next(i for i, c in enumerate(contents) if "Cast of characters" in c)
        assert genesis_index < cast_index
        assert "性格: 冷静" in contents[cast_index]
        assert "语言风格: 短句" in contents[cast_index]
        manager.set_cast([Character(name="Alice", personality="暴躁")])
        await manager.generate_resolution({"Alice": "Wait"})
        contents = [message["content"] for message in client.calls[-1]["messages"]]
        assert any("性格: 暴躁" in c for c in contents)
        assert not any("性格: 冷静" in c for c in contents)

    asyncio.run(run())


def test_start_state_includes_card_fields():
    """The opening prompt carries the full card text."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    client = FakeClient(
        lambda kwargs: RoundResolution(
            global_narrative="Alice stands at the gate.", player_resolutions={}
        )
    )

    async def run():
        manager = LLMContextManager(client)
        manager.set_genesis("A gate.")
        await manager.generate_start_state(
            [Character(name="Alice", personality="冷静", example_dialogue="站住。")],
            {"Alice": "Alice"},
        )
        prompt = client.calls[-1]["messages"][-1]["content"]
        assert "性格: 冷静" in prompt
        assert "示例台词: 站住。" in prompt

    asyncio.run(run())


async def started_card_game(tmp_path):
    """Build a started game whose cast has card fields."""
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
            characters=[
                {"name": "Host", "personality": "稳重"},
                {"name": "Player", "description": "水手"},
            ],
            host_character="Host",
        ),
    )
    await engine.wait_for_inference()
    await engine.player_join("player", {"name": "Player", "character": "Player"})
    await engine.process_payload("host", payload("start_game"))
    await engine.wait_for_inference()
    return engine, sender, resolver


def test_player_can_edit_their_own_card(tmp_path):
    """A claimed character's card is editable by its owner mid-game."""

    async def run():
        engine, sender, resolver = await started_card_game(tmp_path)
        await engine.process_payload(
            "player",
            payload(
                "character_update",
                personality="鲁莽",
                style="大喊大叫",
                example_dialogue="冲啊！",
                description="改了的水手",
            ),
        )
        card = next(char for char in engine.cast if char.name == "Player")
        assert card.personality == "鲁莽"
        assert card.style == "大喊大叫"
        assert card.example_dialogue == "冲啊！"
        assert card.description == "改了的水手"
        assert resolver.cast_cards[-1].personality == "鲁莽"
        messages = sender.events_of_type("system_msg")
        assert any("更新了角色卡" in event.payload["msg"] for event in messages)
        roster = sender.events_of_type("player_roster")[-1].payload
        mine = next(char for char in roster["characters"] if char["name"] == "Player")
        assert mine["personality"] == "鲁莽"
        await engine.shutdown()

    asyncio.run(run())


def test_card_edits_are_rejected_without_ownership(tmp_path):
    """Spectators and non-owners cannot edit a card."""

    async def run():
        engine, sender, _ = await started_card_game(tmp_path)
        await engine.player_join("viewer", {"name": "Viewer"})
        await engine.process_payload("viewer", payload("character_update", personality="偷改"))
        error = sender.events_of_type("error")[-1].payload["msg"]
        assert "not playing a character" in error
        # Simulate a seat whose claim is held by a different name.
        engine.claims["Player"] = "SomeoneElse"
        await engine.process_payload("player", payload("character_update", personality="抢改"))
        error = sender.events_of_type("error")[-1].payload["msg"]
        assert "do not own" in error
        card = next(char for char in engine.cast if char.name == "Host")
        assert card.personality == "稳重"
        await engine.shutdown()

    asyncio.run(run())


def test_card_fields_survive_persistence(tmp_path):
    """Edited cards are restored after a room save/load round-trip."""

    async def run():
        engine, sender, _ = await started_card_game(tmp_path)
        await engine.process_payload("player", payload("character_update", personality="鲁莽"))
        restored = restore_engine(engine.to_persistent_dict(), sender, FakeResolver)
        card = next(char for char in restored.cast if char.name == "Player")
        assert card.personality == "鲁莽"
        assert restored.resolver.cast_cards[-1].personality == "鲁莽"
        await engine.shutdown()
        await restored.shutdown()

    asyncio.run(run())

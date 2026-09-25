"""World-book entries: parsing, keyword injection, budget, and persistence."""

import asyncio

import pytest

from core.config import settings
from core.schemas import LorebookEntry
from logic.engine import GameEngine, restore_engine
from logic.llm_manager import LLMContextManager
from logic.lobby import parse_lorebook
from logic.transcript import GameTranscript

from test_engine import FakeResolver, FakeSender, payload
from test_priority_one_llm import FakeClient


def test_parse_lorebook_validation():
    """World-book entries are validated strictly."""
    entries = parse_lorebook(
        [
            {"title": "酒窖", "keys": ["酒窖", "地下室"], "content": "酒窖里有暗门", "order": 5},
            {"keys": ["常驻"], "content": "这里是冬日", "constant": True},
        ]
    )
    assert entries[0].title == "酒窖"
    assert entries[0].keys == ["酒窖", "地下室"]
    assert entries[1].constant
    with pytest.raises(ValueError):
        parse_lorebook([{"keys": [], "content": "x"}])
    with pytest.raises(ValueError):
        parse_lorebook([{"keys": ["a", "a"], "content": "x"}])
    with pytest.raises(ValueError):
        parse_lorebook([{"keys": ["a"], "content": "x", "enabled": "yes"}])
    with pytest.raises(ValueError):
        parse_lorebook([{"keys": ["a"]}])
    with pytest.raises(ValueError):
        parse_lorebook("not-a-list")
    with pytest.raises(ValueError):
        parse_lorebook([{"keys": [f"k{i}" for i in range(101)], "content": "x"}])
    assert parse_lorebook([{"keys": [f"k{i}" for i in range(100)], "content": "x"}])[0].keys


def setup_manager(client, lorebook):
    """Build a manager with genesis and lorebook configured."""
    manager = LLMContextManager(client)
    manager.set_genesis("A village in winter.")
    manager.set_lorebook(lorebook)
    return manager


def test_keyword_match_injects_entry_but_not_into_history():
    """A matched entry reaches the request; the stored history stays clean."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    client = FakeClient()

    async def run():
        manager = setup_manager(
            client, [LorebookEntry(keys=["铁匠"], content="铁匠铺在村北，炉火通明。", order=100)]
        )
        await manager.generate_resolution({"Alice": "去铁匠铺看看"})
        content = client.calls[-1]["messages"][-1]["content"]
        assert "World lore" in content
        assert "铁匠铺在村北" in content
        assert not any("World lore" in message.get("content", "") for message in manager.history)

    asyncio.run(run())


def test_unmatched_entry_is_not_injected():
    """Without a key match the lorebook block is absent."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    client = FakeClient()

    async def run():
        manager = setup_manager(client, [LorebookEntry(keys=["铁匠"], content="铁匠铺在村北。")])
        await manager.generate_resolution({"Alice": "去河边散步"})
        content = client.calls[-1]["messages"][-1]["content"]
        assert "World lore" not in content

    asyncio.run(run())


def test_constant_entries_are_always_injected():
    """Constant entries need no keywords."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    client = FakeClient()

    async def run():
        manager = setup_manager(
            client,
            [
                LorebookEntry(keys=["无关"], content="不会出现。"),
                LorebookEntry(keys=[], content="世界正在下雪。", constant=True),
            ],
        )
        await manager.generate_resolution({"Alice": "看看窗外"})
        content = client.calls[-1]["messages"][-1]["content"]
        assert "世界正在下雪" in content
        assert "不会出现" not in content

    asyncio.run(run())


def test_recursive_activation_pulls_related_entries():
    """A matched entry whose content mentions another entry's key pulls it in."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    client = FakeClient()

    async def run():
        manager = setup_manager(
            client,
            [
                LorebookEntry(keys=["黑鸦"], content="黑鸦群常在酒窖附近盘旋。"),
                LorebookEntry(keys=["酒窖"], content="酒窖深处藏着一扇暗门。"),
            ],
        )
        await manager.generate_resolution({"Alice": "观察黑鸦的动向"})
        content = client.calls[-1]["messages"][-1]["content"]
        assert "黑鸦群常在酒窖附近盘旋" in content
        assert "酒窖深处藏着一扇暗门" in content

    asyncio.run(run())


def test_budget_caps_matched_entries_by_order():
    """The token budget keeps high-order entries and drops the rest."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    settings.llm.lorebook_max_tokens = 64
    client = FakeClient()

    async def run():
        manager = setup_manager(
            client,
            [
                LorebookEntry(keys=["市场"], content="低价情报" * 10, order=10),
                LorebookEntry(keys=["市场"], content="高价线索" * 10, order=20),
            ],
        )
        await manager.generate_resolution({"Alice": "在市场闲逛"})
        content = client.calls[-1]["messages"][-1]["content"]
        assert "高价线索" in content
        assert "低价情报" not in content

    asyncio.run(run())


def test_disabled_entries_never_activate():
    """Disabled entries are skipped even with a key match."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    client = FakeClient()

    async def run():
        manager = setup_manager(
            client, [LorebookEntry(keys=["铁匠"], content="铁匠铺在村北。", enabled=False)]
        )
        await manager.generate_resolution({"Alice": "去铁匠铺"})
        content = client.calls[-1]["messages"][-1]["content"]
        assert "铁匠铺在村北" not in content

    asyncio.run(run())


def test_scan_depth_limits_history_matching():
    """Keys beyond the scan depth do not activate entries."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    settings.llm.lorebook_scan_depth = 1
    client = FakeClient()

    async def run():
        manager = setup_manager(client, [LorebookEntry(keys=["铁匠"], content="铁匠铺在村北。")])
        manager.history = [
            {"role": "user", "content": "昨天去过铁匠铺"},
            {"role": "assistant", "content": "记不清了。"},
        ]
        await manager.generate_resolution({"Alice": "现在去河边"})
        content = client.calls[-1]["messages"][-1]["content"]
        assert "铁匠铺在村北" not in content

    asyncio.run(run())


def test_lorebook_flows_through_engine_and_persistence(tmp_path):
    """scenario_init stores the lorebook on engine, resolver, and saves."""

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
                scenario="A village.",
                characters=[{"name": "Host"}],
                host_character="Host",
                lorebook=[{"keys": ["酒窖"], "content": "酒窖有秘密", "order": 5}],
            ),
        )
        await engine.wait_for_inference()
        assert [entry.keys for entry in engine.lorebook] == [["酒窖"]]
        assert [entry.content for entry in resolver.lorebook] == ["酒窖有秘密"]
        restored = restore_engine(engine.to_persistent_dict(), sender, FakeResolver)
        assert [entry.keys for entry in restored.lorebook] == [["酒窖"]]
        assert [entry.content for entry in restored.resolver.lorebook] == ["酒窖有秘密"]
        await engine.shutdown()
        await restored.shutdown()

    asyncio.run(run())


def test_invalid_lorebook_rejects_scenario(tmp_path):
    """Malformed lorebook fails scenario_init without changing state."""

    async def run():
        sender = FakeSender()
        engine = GameEngine(sender, FakeResolver())
        engine.transcript = GameTranscript(tmp_path / "logs")
        await engine.host_join("host", {"name": "Host"})
        await engine.process_payload(
            "host",
            payload(
                "scenario_init",
                scenario="A village.",
                characters=[{"name": "Host"}],
                host_character="Host",
                lorebook=[{"keys": ["a"], "content": "x", "order": -5}],
            ),
        )
        error = sender.events_of_type("error")[-1].payload["msg"]
        assert "world-book order" in error
        await engine.shutdown()

    asyncio.run(run())

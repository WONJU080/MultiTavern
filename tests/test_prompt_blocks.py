"""Prompt blocks and per-room sampling: parsing, injection, and privacy."""

import asyncio

import pytest

from core.config import settings
from core.schemas import PromptBlock, RoundResolution, SamplingConfig
from logic.engine import GameEngine, restore_engine
from logic.llm_manager import LLMContextManager, LLMResolutionError
from logic.lobby import parse_prompt_blocks, parse_sampling
from logic.transcript import GameTranscript

from test_engine import FakeResolver, FakeSender, payload
from test_priority_one_llm import FakeClient


def test_parse_prompt_blocks_and_sampling_validation():
    """Block positions are constrained and sampling values are bounded."""
    blocks = parse_prompt_blocks(
        [
            {"title": "文风", "content": "简洁。", "position": "scenario"},
            {"content": "第三人称。", "position": "output", "enabled": False},
        ]
    )
    assert blocks[0].position == "scenario" and blocks[0].enabled
    assert not blocks[1].enabled and blocks[1].position == "output"
    with pytest.raises(ValueError):
        parse_prompt_blocks([{"content": "x", "position": "nowhere"}])
    with pytest.raises(ValueError):
        parse_prompt_blocks([{"content": "x", "enabled": "yes"}])
    sampling = parse_sampling({"temperature": 0.7, "top_p": 0.9})
    assert sampling.temperature == 0.7 and sampling.top_p == 0.9
    with pytest.raises(ValueError):
        parse_sampling({"temperature": 3})
    with pytest.raises(ValueError):
        parse_sampling({"top_p": "high"})
    assert parse_sampling(None) == SamplingConfig()


def test_parse_prompt_blocks_accepts_history_role_and_depth():
    """history blocks carry a role and a bounded depth; older blocks keep defaults."""
    blocks = parse_prompt_blocks(
        [{"content": "提醒。", "position": "history", "role": "assistant", "depth": 4}]
    )
    assert blocks[0].position == "history"
    assert blocks[0].role == "assistant"
    assert blocks[0].depth == 4
    default = parse_prompt_blocks([{"content": "x"}])[0]
    assert default.position == "output" and default.role == "system" and default.depth == 0
    with pytest.raises(ValueError):
        parse_prompt_blocks([{"content": "x", "position": "history", "role": "nobody"}])
    with pytest.raises(ValueError):
        parse_prompt_blocks([{"content": "x", "depth": 101}])
    with pytest.raises(ValueError):
        parse_prompt_blocks([{"content": "x", "depth": True}])


def build_manager(client):
    """Create a manager with genesis and prompt configuration."""
    manager = LLMContextManager(client)
    manager.set_genesis("A village in winter.")
    manager.set_prompt_blocks(
        [
            PromptBlock(content="[系统块] DM 协议。", position="system"),
            PromptBlock(content="[情景块] 写作风格。", position="scenario"),
            PromptBlock(content="[输出块] 第三人称。", position="output"),
            PromptBlock(content="[关闭块] 不要出现。", position="output", enabled=False),
        ]
    )
    return manager


def test_blocks_land_at_their_positions_and_disabled_blocks_stay_out():
    """Enabled blocks appear at their configured positions; disabled ones never do."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    client = FakeClient()

    async def run():
        manager = build_manager(client)
        await manager.generate_resolution({"Alice": "Wait"})
        messages = client.calls[-1]["messages"]
        contents = [message["content"] for message in messages]
        system_index = next(i for i, content in enumerate(contents) if "[系统块]" in content)
        scenario_index = next(i for i, content in enumerate(contents) if "[情景块]" in content)
        output_index = next(i for i, content in enumerate(contents) if "[输出块]" in content)
        genesis_index = next(
            i for i, content in enumerate(contents) if "A village in winter" in content
        )
        assert system_index < genesis_index
        assert genesis_index < scenario_index
        assert scenario_index < output_index
        assert output_index == len(messages) - 1
        assert not any("[关闭块]" in content for content in contents)

    asyncio.run(run())


def test_history_blocks_are_interleaved_at_depth_with_role():
    """history blocks land at the configured depth and role."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    client = FakeClient()

    async def run():
        manager = build_manager(client)
        manager.history[:] = [
            {"role": "user", "content": "旧输入一"},
            {"role": "assistant", "content": "旧输出一"},
            {"role": "user", "content": "旧输入二"},
        ]
        manager.set_prompt_blocks(
            [
                PromptBlock(content="[贴近提醒]", position="history", role="system", depth=0),
                PromptBlock(content="[假助手回合]", position="history", role="assistant", depth=0),
                PromptBlock(content="[更深提醒]", position="history", role="user", depth=2),
                PromptBlock(content="[关闭的历史块]", position="history", depth=1, enabled=False),
            ]
        )
        await manager.generate_resolution({"Alice": "Wait"})
        messages = client.calls[-1]["messages"]
        contents = [message["content"] for message in messages]
        roles = [message["role"] for message in messages]

        deep_index = contents.index("[更深提醒]")
        assert roles[deep_index] == "user"
        assert contents[deep_index - 1] == "旧输入一"
        assert contents[deep_index + 1] == "旧输出一"

        remind_index = contents.index("[贴近提醒]")
        assistant_index = contents.index("[假助手回合]")
        assert roles[remind_index] == "system"
        assert roles[assistant_index] == "assistant"
        assert remind_index + 1 == assistant_index
        assert assistant_index + 1 == len(messages) - 1
        assert not any("[关闭的历史块]" in content for content in contents)

    asyncio.run(run())


def test_output_blocks_skip_dice_planning():
    """Style instructions apply to narrative generation, not dice planning."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    client = FakeClient()

    async def run():
        manager = build_manager(client)
        await manager.plan_dice({"Alice": "Wait"})
        messages = client.calls[-1]["messages"]
        assert not any("[输出块]" in message["content"] for message in messages)
        assert any("[系统块]" in message["content"] for message in messages)
        await manager.generate_resolution({"Alice": "Wait"})
        messages = client.calls[-1]["messages"]
        assert any("[输出块]" in message["content"] for message in messages)

    asyncio.run(run())


def test_sampling_options_are_forwarded_to_generation():
    """Per-room temperature and top_p reach the provider call."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    client = FakeClient()

    async def run():
        manager = build_manager(client)
        manager.set_sampling(SamplingConfig(temperature=0.7, top_p=0.9))
        await manager.generate_resolution({"Alice": "Wait"})
        kwargs = client.calls[-1]
        assert kwargs["temperature"] == 0.7
        assert kwargs["top_p"] == 0.9

    asyncio.run(run())


def test_unset_sampling_omits_parameters():
    """Without overrides, no sampling parameters are sent."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    client = FakeClient()

    async def run():
        manager = build_manager(client)
        await manager.generate_resolution({"Alice": "Wait"})
        kwargs = client.calls[-1]
        assert "temperature" not in kwargs and "top_p" not in kwargs

    asyncio.run(run())


def test_parse_sampling_extra_fields_and_forwarding():
    """Extra samplers parse, range-check, and travel through extra_body."""
    sampling = parse_sampling(
        {
            "frequency_penalty": 0.5,
            "presence_penalty": -0.5,
            "top_k": 40,
            "min_p": 0.05,
            "repetition_penalty": 1.1,
        }
    )
    assert sampling.frequency_penalty == 0.5 and sampling.presence_penalty == -0.5
    assert sampling.top_k == 40 and sampling.min_p == 0.05
    assert sampling.repetition_penalty == 1.1
    with pytest.raises(ValueError):
        parse_sampling({"top_k": -1})
    with pytest.raises(ValueError):
        parse_sampling({"min_p": 2})

    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    client = FakeClient()

    async def run():
        manager = build_manager(client)
        manager.set_sampling(sampling)
        await manager.generate_resolution({"Alice": "Wait"})
        kwargs = client.calls[-1]
        assert kwargs["frequency_penalty"] == 0.5
        assert kwargs["presence_penalty"] == -0.5
        assert kwargs["extra_body"]["top_k"] == 40
        assert kwargs["extra_body"]["min_p"] == 0.05
        assert kwargs["extra_body"]["repetition_penalty"] == 1.1

    asyncio.run(run())


def test_extra_samplers_fall_back_on_bad_request():
    """A backend that rejects extra samplers is retried without them once."""
    import httpx
    from openai import BadRequestError

    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True

    class RejectingClient(FakeClient):
        async def parse(self, **kwargs):
            if kwargs.get("extra_body", {}).get("top_k") is not None:
                request = httpx.Request("POST", "http://test/v1/chat/completions")
                raise BadRequestError(
                    "rejected", response=httpx.Response(400, request=request), body=None
                )
            return await super().parse(**kwargs)

    client = RejectingClient()

    async def run():
        manager = build_manager(client)
        manager.set_sampling(SamplingConfig(temperature=0.9, top_k=200))
        await manager.generate_resolution({"Alice": "Wait"})
        assert manager.sampling_extra == {}
        assert manager._extra_samplers_rejected is True
        assert len(client.calls) == 1
        assert "extra_body" not in client.calls[-1]
        # A later set_sampling must not resurrect the rejected fields.
        manager.set_sampling(SamplingConfig(top_k=300))
        assert manager.sampling_extra == {}

    asyncio.run(run())


def test_output_echoing_a_block_is_rejected():
    """Quoting an instruction block in public output fails the round."""
    settings.llm.provider = "openai"
    settings.llm.structured_outputs = True
    secret = "输出必须使用第三人称描述所有角色行为"
    client = FakeClient(
        lambda kwargs: RoundResolution(
            global_narrative=f"整段照抄指令原文：{secret}。",
            player_resolutions={"Alice": "Alice waits."},
        )
    )

    async def run():
        manager = build_manager(client)
        manager.set_prompt_blocks([PromptBlock(content=secret, position="output")])
        with pytest.raises(LLMResolutionError, match="泄露了私密指引"):
            await manager.generate_resolution({"Alice": "Wait"})

    asyncio.run(run())


def test_blocks_and_sampling_flow_through_engine_and_persistence(tmp_path):
    """scenario_init stores blocks and sampling; they survive a save/load."""

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
                prompt_blocks=[
                    {
                        "title": "文风",
                        "content": "简洁。",
                        "position": "history",
                        "role": "assistant",
                        "depth": 3,
                    }
                ],
                sampling={"temperature": 0.8, "top_p": 0.9},
            ),
        )
        await engine.wait_for_inference()
        assert [block.content for block in engine.prompt_blocks] == ["简洁。"]
        assert engine.sampling.temperature == 0.8
        assert [block.content for block in resolver.prompt_blocks] == ["简洁。"]
        assert resolver.sampling.temperature == 0.8
        restored = restore_engine(engine.to_persistent_dict(), sender, FakeResolver)
        assert [block.position for block in restored.prompt_blocks] == ["history"]
        assert restored.prompt_blocks[0].role == "assistant"
        assert restored.prompt_blocks[0].depth == 3
        assert restored.sampling.top_p == 0.9
        assert restored.resolver.sampling.temperature == 0.8
        await engine.shutdown()
        await restored.shutdown()

    asyncio.run(run())


def test_invalid_blocks_reject_scenario(tmp_path):
    """Malformed prompt blocks fail scenario_init without changing state."""

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
                prompt_blocks=[{"content": "x", "position": "middle"}],
            ),
        )
        error = sender.events_of_type("error")[-1].payload["msg"]
        assert "未知的提示块位置" in error
        await engine.shutdown()

    asyncio.run(run())

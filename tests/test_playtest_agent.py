"""Seat brains: prompt building, reply cleaning and provider calls."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace
from typing import Any

import pytest

from core.schemas import Character
from playtest.agent import (
    DEFAULT_ACTION_LIMIT,
    STYLE_HINTS,
    OpenAICompatBrain,
    SeatAuthError,
    use_system_proxy,
    ScriptedBrain,
    SeatError,
    SeatTurn,
    build_system_prompt,
    build_turn_prompt,
    character_card,
    clean_action,
    fallback_action,
)
from playtest.config import SeatLLM

LOCKSMITH = Character(
    name="甲",
    description="谨慎的锁匠，右手有旧伤。",
    personality="话少，先看后动。",
    style="短句，带工具比喻。",
    example_dialogue="「锁芯是好的，别硬拧。」",
)


def fake_client(result: Any) -> Any:
    """Return a chat-completions stub that records the options it receives."""

    class _Completions:
        def __init__(self) -> None:
            self.calls: list[dict[str, Any]] = []

        async def create(self, **options: Any) -> Any:
            self.calls.append(options)
            if isinstance(result, Exception):
                raise result
            return result

    completions = _Completions()
    return SimpleNamespace(chat=SimpleNamespace(completions=completions))


def response_with(text: str) -> Any:
    """Build a minimal chat completion response."""
    return SimpleNamespace(choices=[SimpleNamespace(message=SimpleNamespace(content=text))])


def test_clean_action_strips_model_furniture() -> None:
    """Markdown, labels, quotes and OOC notes never reach the game."""
    assert clean_action("行动：我推开门走进去。") == "我推开门走进去。"
    assert clean_action("```\n我推开门走进去。\n```") == "我推开门走进去。"
    assert clean_action("「我推开门走进去。」") == "我推开门走进去。"
    assert clean_action("(OOC: 让我想想) 我推开门。") == "我推开门。"
    assert clean_action("OOC: 先分析一下\n我推开门。") == "我推开门。"
    assert clean_action("  我   推开门  ") == "我 推开门"


def test_clean_action_keeps_a_closing_dialogue_bracket() -> None:
    """An action that ends with a spoken line must keep its closing bracket."""
    assert clean_action("我问邱明：「你几点到的？」") == "我问邱明：「你几点到的？」"
    # A fully quoted action is still unwrapped.
    assert clean_action("「我推了推门。」") == "我推了推门。"


def test_a_sprawling_reply_is_cut_to_whole_sentences() -> None:
    """Too-long input is trimmed to complete clauses, not cut mid-quote."""
    sprawl = (
        "我先走到桌边，把登记簿翻到我们五个人的名字那一页，手指按在上面，"
        "抬头问孔繁声：「你比我们早到，老周的名字什么时候从这头上不见的？"
        "——还有那把钥匙板，少了的那两把，你知道是开哪儿的吗？"
    )

    cleaned = clean_action(sprawl)

    assert len(cleaned) <= DEFAULT_ACTION_LIMIT, cleaned
    assert cleaned.count("「") == cleaned.count("」"), cleaned
    assert cleaned.endswith("。")


def test_a_run_on_reply_is_cut_at_a_clause_boundary() -> None:
    """Without sentence breaks, the cut still lands on a natural pause."""
    run_on = (
        "我先走到桌边把登记簿翻到最后一页，逐行核对上面五个人的签名和笔迹深浅，"
        "顺便抬头观察每个人的表情，然后问孔繁声他到底是什么时候到的，"
        "同时留意墙上钥匙板剩下的钥匙有没有被谁动过，再记下炉子边的棉大衣挂在哪个位置。"
    )

    cleaned = clean_action(run_on)

    assert len(cleaned) <= DEFAULT_ACTION_LIMIT
    assert cleaned.endswith("。")
    assert "钥匙板" not in cleaned  # the tail was dropped, not cut mid-clause


def test_the_default_action_limit_is_short() -> None:
    """A player types a line, not a paragraph."""
    assert DEFAULT_ACTION_LIMIT <= 100


def test_clean_action_rejects_empty_and_truncates_long_replies() -> None:
    """Empty replies are surfaced as failures, and long ones are cut cleanly."""
    assert clean_action("   \n  ") == ""
    assert clean_action(None) == ""  # type: ignore[arg-type]
    long_text = "啊" * 500
    cleaned = clean_action(long_text, limit=50)
    assert len(cleaned) == 50
    assert cleaned.endswith("…")


def test_prompts_carry_the_character_and_the_contract() -> None:
    """A seat prompt states the character, the style and the output rules."""
    system = build_system_prompt(LOCKSMITH, "explore", 300)

    assert "甲" in system
    assert "锁匠" in system
    assert "话少" in system
    assert STYLE_HINTS["explore"] in system
    assert "300" in system
    assert "不是旁白" in system
    assert character_card(LOCKSMITH).startswith("姓名：甲")

    turn = build_turn_prompt(
        SeatTurn(character="甲", style="explore", digest="【当前】轮到你了", round_number=2)
    )
    assert "第 2 回合" in turn
    assert "【当前】轮到你了" in turn


def test_turn_prompt_explains_a_rejection() -> None:
    """A rejected action is retried with the server's reason attached."""
    turn = SeatTurn(
        character="甲",
        style="bold",
        digest="【当前】轮到你了",
        round_number=1,
        rejection="行动描述不够具体。",
    )

    prompt = build_turn_prompt(turn)

    assert "行动描述不够具体。" in prompt
    assert "【当前】轮到你了" in prompt


def test_scripted_brain_varies_by_round_and_never_returns_empty() -> None:
    """The offline seat produces usable, non-repeating actions."""
    brain = ScriptedBrain()

    async def run() -> list[str]:
        actions = []
        for number in (1, 2, 3):
            actions.append(
                await brain.act(
                    SeatTurn(character="甲", style="explore", digest="", round_number=number)
                )
            )
        return actions

    actions = asyncio.run(run())

    assert len(set(actions)) == 3
    assert all(actions)
    assert brain.calls == 3


def test_fallback_actions_read_like_player_input() -> None:
    """The deterministic fallback is player-voiced, and varies by round."""
    first = fallback_action(LOCKSMITH, 1)
    second = fallback_action(LOCKSMITH, 2)

    assert first.startswith("我") and second.startswith("我")
    assert "甲" not in first and "甲" not in second
    assert first != second


def test_openai_brain_cleans_the_reply_and_uses_the_compatible_cap() -> None:
    """A compatible endpoint is called with max_tokens and a bounded seat prompt."""
    client = fake_client(response_with("行动：我检查门锁。"))
    seat = SeatLLM(
        endpoint="https://api.example.com/v1",
        api_key="k",
        model_name="m",
        temperature=0.9,
        provider="compatible",
    )
    brain = OpenAICompatBrain(seat, LOCKSMITH, "cautious", client=client)

    action = asyncio.run(
        brain.act(SeatTurn(character="甲", style="cautious", digest="状态", round_number=1))
    )

    assert action == "我检查门锁。"
    options = client.chat.completions.calls[0]
    assert options["model"] == "m"
    assert options["max_tokens"] == seat.max_output_tokens
    assert options["temperature"] == 0.9
    assert options["messages"][0]["role"] == "system"
    assert "状态" in options["messages"][1]["content"]


def test_openai_brain_uses_max_completion_tokens_for_openai() -> None:
    """The OpenAI provider needs its own output-cap keyword."""
    client = fake_client(response_with("我检查门锁。"))
    seat = SeatLLM(
        endpoint="https://api.openai.com/v1", api_key="k", model_name="gpt", provider="openai"
    )
    brain = OpenAICompatBrain(seat, LOCKSMITH, "bold", client=client)

    asyncio.run(brain.act(SeatTurn(character="甲", style="bold", digest="", round_number=1)))

    options = client.chat.completions.calls[0]
    assert "max_completion_tokens" in options
    assert "max_tokens" not in options
    assert "temperature" not in options


@pytest.mark.parametrize(
    "result, expected",
    [
        (response_with("   "), "返回了空行动"),
        (RuntimeError("provider exploded"), "调用失败"),
    ],
)
def test_openai_brain_surfaces_failures_as_seat_errors(result: Any, expected: str) -> None:
    """Empty replies and provider errors both stop one seat, not the run."""
    seat = SeatLLM(endpoint="https://api.example.com/v1", api_key="k", model_name="m")
    brain = OpenAICompatBrain(seat, LOCKSMITH, "bold", client=fake_client(result))

    with pytest.raises(SeatError) as excinfo:
        asyncio.run(brain.act(SeatTurn(character="甲", style="bold", digest="", round_number=1)))

    assert expected in str(excinfo.value)


def test_openai_brain_refuses_to_guess_an_endpoint() -> None:
    """An empty endpoint must not silently call OpenAI's official API."""
    seat = SeatLLM(api_key="k", model_name="deepseek-chat")  # compatible provider
    brain = OpenAICompatBrain(seat, LOCKSMITH, "bold")

    with pytest.raises(SeatError) as excinfo:
        asyncio.run(brain.act(SeatTurn(character="甲", style="bold", digest="", round_number=1)))

    assert "endpoint" in str(excinfo.value)


def test_a_rejected_key_is_a_distinct_error() -> None:
    """401 responses are classified so the runner can stop early."""
    error = RuntimeError("Incorrect API key provided")
    error.status_code = 401  # type: ignore[attr-defined]
    seat = SeatLLM(endpoint="https://api.example.com/v1", api_key="k", model_name="m")
    brain = OpenAICompatBrain(seat, LOCKSMITH, "bold", client=fake_client(error))

    with pytest.raises(SeatAuthError) as excinfo:
        asyncio.run(brain.act(SeatTurn(character="甲", style="bold", digest="", round_number=1)))

    assert "401" in str(excinfo.value)


@pytest.mark.parametrize(
    "endpoint, proxy, expected",
    [
        # Local endpoints are always direct: a proxy cannot route to loopback.
        ("http://127.0.0.1:8080/v1", None, False),
        ("http://localhost:11434/v1", None, False),
        ("http://192.168.1.20:8080/v1", None, False),
        ("http://10.0.0.5:8080/v1", None, False),
        # Public endpoints follow the environment unless told otherwise.
        ("https://api.deepseek.com/v1", None, True),
        ("https://api.deepseek.com/v1", True, True),
        ("https://api.deepseek.com/v1", False, False),
        ("http://127.0.0.1:8080/v1", True, True),
    ],
)
def test_seat_proxy_policy(endpoint: str, proxy: bool | None, expected: bool) -> None:
    """Local seat endpoints bypass the system proxy; the rest is configurable."""
    assert use_system_proxy(SeatLLM(endpoint=endpoint, model_name="m", proxy=proxy)) is expected


def test_a_direct_seat_client_ignores_proxy_environment(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A local endpoint must not be handed to the system proxy."""
    monkeypatch.setenv("ALL_PROXY", "socks5://127.0.0.1:1")
    seat = SeatLLM(endpoint="http://127.0.0.1:8080/v1", api_key="k", model_name="m")
    brain = OpenAICompatBrain(seat, LOCKSMITH, "bold")

    client = brain._get_client()  # noqa: SLF001 - the policy lives in the client

    assert client._client.trust_env is False  # noqa: SLF001 - httpx client inside the SDK
    asyncio.run(brain.aclose())


def test_fallback_actions_are_concrete_and_varied() -> None:
    """Fallbacks must be executable actions a strict referee can accept."""
    first = fallback_action(LOCKSMITH, 1, 1)
    second = fallback_action(LOCKSMITH, 1, 2)

    assert first != second
    # Player voice: first person, never a third-person line with the name prefix.
    assert first.startswith("我")
    assert "甲" not in first
    # Short and concrete, like something a player would type.
    for action in (first, second):
        assert 6 <= len(action) <= 40, action


def test_the_seat_prompt_demands_an_executable_action() -> None:
    """The prompt itself must rule out vague intent, or referees refuse it."""
    prompt = build_system_prompt(LOCKSMITH, "bold", 300)

    assert "具体可执行" in prompt
    assert "意图" in prompt


def test_the_seat_prompt_asks_for_a_short_line() -> None:
    """Length is part of the player-voice contract."""
    prompt = build_system_prompt(LOCKSMITH, "bold", 80)

    assert "80 字" in prompt
    assert "尽量 40 字以内" in prompt
    assert "太长" in prompt


def test_the_seat_prompt_teaches_player_voice() -> None:
    """A seat must sound like a player typing an action, not like the DM."""
    prompt = build_system_prompt(LOCKSMITH, "bold", 300)

    assert "第一人称" in prompt
    assert "不是旁白" in prompt
    assert "第三人称旁白" in prompt
    assert "像玩家一样打字" in prompt
    # Good and bad examples are what actually move the style.
    assert "✅" in prompt and "❌" in prompt


def test_the_seat_prompt_forbids_declaring_outcomes() -> None:
    """Writing the result of your own action is the DM's job, not the player's."""
    prompt = build_system_prompt(LOCKSMITH, "bold", 300)

    assert "尝试" in prompt
    assert "结果" in prompt
    assert "由 DM 判定" in prompt
    assert "编造" in prompt  # inventing settings the DM never wrote
    assert "发现里面有一具尸体" in prompt  # the concrete wrong example


def test_the_seat_prompt_tells_it_this_is_a_game() -> None:
    """Explicitly: this is a game turn, not novel writing."""
    prompt = build_system_prompt(LOCKSMITH, "bold", 300)

    assert "不是写小说" in prompt
    assert "游戏" in prompt


def test_the_seat_prompt_says_how_to_react_to_others() -> None:
    """Seats must know they can see and answer other players."""
    prompt = build_system_prompt(LOCKSMITH, "bold", 300)

    assert "本轮其他人已经声明的行动" in prompt
    assert "等你回应的提议" in prompt
    assert "替他们写结果" in prompt


def test_the_turn_prompt_ends_with_the_voice_rule() -> None:
    """The last thing a seat reads is how to write the action."""
    turn = SeatTurn(character="甲", style="bold", digest="【剧本】测试", round_number=2)

    prompt = build_turn_prompt(turn)

    assert prompt.startswith("【剧本】测试")
    assert prompt.rstrip().endswith("不要替其他角色行动或说话。")
    assert "第一人称" in prompt
    assert "第 2 回合" in prompt


def response_with_parts(
    content: str, *, reasoning: str = "", finish: str = "stop", completion_tokens: int = 10
) -> Any:
    """Build a reply with reasoning, a finish reason and usage."""
    message = SimpleNamespace(content=content, reasoning_content=reasoning)
    choice = SimpleNamespace(message=message, finish_reason=finish)
    usage = SimpleNamespace(completion_tokens=completion_tokens)
    return SimpleNamespace(choices=[choice], usage=usage)


class SequenceClient:
    """A client that returns queued replies and records the options it was given."""

    def __init__(self, replies: list[Any]) -> None:
        """Store the replies in order."""
        self.replies = replies
        outer = self

        class _Completions:
            def __init__(self) -> None:
                self.calls: list[dict[str, Any]] = []

            async def create(self, **options: Any) -> Any:
                self.calls.append(options)
                return outer.replies.pop(0)

        self.completions = _Completions()

    @property
    def chat(self) -> Any:
        """Expose the completions API the SDK offers."""
        return SimpleNamespace(completions=self.completions)


def test_a_truncated_reply_is_retried_with_more_room() -> None:
    """A reasoning model may spend the whole cap before writing the action."""
    client = SequenceClient(
        [
            response_with_parts("", reasoning="思考" * 200, finish="length"),
            response_with_parts("我先走到炉边，检查风门和烟道。"),
        ]
    )
    seat = SeatLLM(
        endpoint="https://api.example.com/v1", api_key="k", model_name="m", max_output_tokens=400
    )
    brain = OpenAICompatBrain(seat, LOCKSMITH, "bold", client=client)

    action = asyncio.run(
        brain.act(SeatTurn(character="甲", style="bold", digest="", round_number=1))
    )

    assert action == "我先走到炉边，检查风门和烟道。"
    caps = [call["max_tokens"] for call in client.completions.calls]
    assert caps == [400, 800], caps


def test_an_empty_reply_explains_itself() -> None:
    """The error must say why the reply was unusable."""
    truncated = response_with_parts(
        "", reasoning="推理" * 50, finish="length", completion_tokens=400
    )
    client = SequenceClient([truncated, truncated])  # the larger-cap retry truncates too
    seat = SeatLLM(
        endpoint="https://api.example.com/v1", api_key="k", model_name="m", max_output_tokens=400
    )
    brain = OpenAICompatBrain(seat, LOCKSMITH, "bold", client=client)

    with pytest.raises(SeatError) as excinfo:
        asyncio.run(brain.act(SeatTurn(character="甲", style="bold", digest="", round_number=1)))

    message = str(excinfo.value)
    assert "推理内容" in message
    assert "截断" in message


def test_raw_replies_can_be_dumped_for_diagnosis() -> None:
    """With debugging on, the seat records what the model actually said."""
    chunks: list[str] = []
    client = SequenceClient([response_with_parts("我先看炉子。")])
    seat = SeatLLM(endpoint="https://api.example.com/v1", api_key="k", model_name="m")
    brain = OpenAICompatBrain(seat, LOCKSMITH, "bold", client=client, response_sink=chunks.append)

    asyncio.run(brain.act(SeatTurn(character="甲", style="bold", digest="", round_number=1)))

    assert chunks and "我先看炉子。" in chunks[0]
    assert "finish_reason=stop" in chunks[0]


def test_openai_brain_requires_a_model_name() -> None:
    """A seat without a model fails before any call is attempted."""
    seat = SeatLLM(endpoint="https://api.example.com/v1", api_key="k")
    brain = OpenAICompatBrain(seat, LOCKSMITH, "bold", client=fake_client(response_with("x")))

    with pytest.raises(SeatError):
        asyncio.run(brain.act(SeatTurn(character="甲", style="bold", digest="", round_number=1)))

"""Possible-truths plugin: parsing, reveal conditions and submission flow."""

import asyncio
from pathlib import Path

import pytest

from core.schemas import ActionTruthFinding, TruthVerdict
from logic.engine import GameEngine, GameState, restore_engine
from logic.plugins import parse_plugins
from logic.transcript import GameTranscript
from test_engine import FakeResolver, FakeSender, payload

TIME_CONFIG = {
    "enabled": True,
    "start_day": 1,
    "start_minute": 390,
    "max_elapsed_minutes": 600,
    "default_elapsed_minutes": 30,
}
EVENTS = [
    {
        "name": "午夜钟声",
        "day": 1,
        "minute": 450,
        "description": "港口的钟声敲响。",
        "public": True,
    }
]


def plugin_config(**overrides: object) -> list[dict[str, object]]:
    """Build a possible-truths plugin with one question per condition."""
    questions = [
        {"id": "q_start", "text": "谁在说谎？", "condition": "start"},
        {"id": "q_round", "text": "谁拿走了钥匙？", "condition": "round", "round_number": 2},
        {
            "id": "q_event",
            "text": "钟声响起时发生了什么？",
            "condition": "event",
            "event_name": "午夜钟声",
        },
        {
            "id": "q_time",
            "text": "午夜的秘密是什么？",
            "condition": "time",
            "day": 1,
            "minute": 450,
        },
    ]
    config: dict[str, object] = {
        "questions": questions,
        "max_answer_chars": 100,
        "require_declarative": True,
        "feed_dm": True,
    }
    config.update(overrides)
    return [{"type": "possible_truths", "enabled": True, "config": config}]


async def build_truth_game(
    tmp_path: Path,
    *,
    plugins: list[dict[str, object]] | None = None,
    host_character: str = "Host",
    time_config: dict[str, object] | None = None,
    events: list[dict[str, object]] | None = None,
) -> tuple[GameEngine, FakeSender, FakeResolver]:
    """Start a two-character mystery game with the possible-truths plugin."""
    sender = FakeSender()
    resolver = FakeResolver()
    engine = GameEngine(sender, resolver)
    engine.transcript = GameTranscript(tmp_path / "logs")
    await engine.host_join("host", {"name": "Host"})
    await engine.process_payload(
        "host",
        payload(
            "scenario_init",
            scenario="Mystery at the harbor.",
            characters=[{"name": "Host"}, {"name": "Player"}],
            host_character=host_character,
            plugins=plugins if plugins is not None else plugin_config(),
            time_config=time_config,
            events=events,
            random_turn_order=False,
        ),
    )
    await engine.wait_for_inference()
    await engine.player_join("player", {"name": "Player", "character": "Player"})
    await engine.process_payload("host", payload("start_game"))
    await engine.wait_for_inference()
    return engine, sender, resolver


def truth_question_ids(sender: FakeSender) -> list[str]:
    """Return the ids of every broadcast question reveal."""
    return [event.payload["id"] for event in sender.events_of_type("truth_question")]


def test_parse_plugins_rejects_invalid_configurations() -> None:
    """Reject unknown types, duplicate ids and contextless event conditions."""
    with pytest.raises(ValueError, match="未知的插件类型"):
        parse_plugins([{"type": "other", "config": {}}])
    with pytest.raises(ValueError, match="id 不能重复"):
        parse_plugins(
            [
                {
                    "type": "possible_truths",
                    "config": {
                        "questions": [
                            {"id": "same", "text": "A"},
                            {"id": "SAME", "text": "B"},
                        ]
                    },
                }
            ]
        )
    with pytest.raises(ValueError, match="event_name"):
        parse_plugins(
            [
                {
                    "type": "possible_truths",
                    "config": {"questions": [{"id": "q", "text": "A", "condition": "event"}]},
                }
            ]
        )
    assert parse_plugins(None) == []


def test_start_question_revealed_and_valid_truth_is_public(tmp_path: Path) -> None:
    """A start question reveals at game start; accepted truths are public."""

    async def run() -> None:
        engine, sender, resolver = await build_truth_game(tmp_path)
        assert truth_question_ids(sender) == ["q_start"]

        await engine.process_payload("player", payload("truth_request"))
        state = sender.events_of_type("truth_state")[-1].payload
        assert state["enabled"] is True
        assert state["round"] == 1
        assert state["your_used_round"] is None
        assert state["max_answer_chars"] == 100
        assert [question["id"] for question in state["questions"]] == ["q_start"]

        resolver.truth_results = [TruthVerdict(valid=True, reason="符合")]
        await engine.process_payload(
            "player", payload("truth_submit", question_id="q_start", answer="管家在隐瞒时间。")
        )
        result = sender.events_of_type("truth_result")[-1]
        assert result.payload["valid"] is True
        answer = sender.events_of_type("truth_answer")[-1].payload
        assert answer == {
            "question_id": "q_start",
            "character": "Player",
            "text": "管家在隐瞒时间。",
            "round": 1,
        }
        assert engine.truth_used_round["Player"] == 1

        await engine.process_payload("player", payload("truth_request"))
        state = sender.events_of_type("truth_state")[-1].payload
        assert state["your_used_round"] == 1
        assert state["questions"][0]["history"] == [
            {"character": "Player", "text": "管家在隐瞒时间。"}
        ]

    asyncio.run(run())


def test_one_submission_per_round_and_round_condition_reveal(tmp_path: Path) -> None:
    """Each player submits once per round; round conditions reveal at commit."""

    async def run() -> None:
        engine, sender, _ = await build_truth_game(tmp_path)
        await engine.process_payload(
            "player", payload("truth_submit", question_id="q_start", answer="甲偷了信。")
        )
        await engine.process_payload(
            "player", payload("truth_submit", question_id="q_start", answer="乙偷了信。")
        )
        assert sender.events_of_type("error")[-1].payload["msg"] == "本轮已经提交过了，请等下一轮。"

        await engine.process_payload("host", payload("action", action="搜身"))
        await engine.process_payload("player", payload("action", action="望风"))
        await engine.wait_for_inference()
        assert engine.round_counter == 1
        assert truth_question_ids(sender) == ["q_start", "q_round"]

        engine.truth_results = []
        await engine.process_payload(
            "player", payload("truth_submit", question_id="q_round", answer="水手拿走了钥匙。")
        )
        assert sender.events_of_type("truth_answer")[-1].payload["text"] == "水手拿走了钥匙。"

    asyncio.run(run())


def test_invalid_truth_consumes_attempt_without_publication(tmp_path: Path) -> None:
    """A rejected truth costs the round's attempt but is not published."""

    async def run() -> None:
        engine, sender, resolver = await build_truth_game(tmp_path)
        resolver.truth_results = [TruthVerdict(valid=False, reason="不是陈述句")]
        await engine.process_payload(
            "player", payload("truth_submit", question_id="q_start", answer="管家是凶手吗？")
        )
        assert sender.events_of_type("truth_result")[-1].payload == {
            "valid": False,
            "reason": "不是陈述句",
        }
        assert not sender.events_of_type("truth_answer")
        assert engine.truth_used_round["Player"] == 1

    asyncio.run(run())


def test_evaluation_failure_does_not_consume_attempt(tmp_path: Path) -> None:
    """A backend failure returns retryable feedback without spending the attempt."""

    async def run() -> None:
        engine, sender, resolver = await build_truth_game(tmp_path)
        resolver.truth_results = [RuntimeError("backend down")]
        await engine.process_payload(
            "player", payload("truth_submit", question_id="q_start", answer="管家在隐瞒时间。")
        )
        result = sender.events_of_type("truth_result")[-1]
        assert result.payload["valid"] is None
        assert "Player" not in engine.truth_used_round

        await engine.process_payload(
            "player", payload("truth_submit", question_id="q_start", answer="管家在隐瞒时间。")
        )
        assert sender.events_of_type("truth_result")[-1].payload["valid"] is True
        assert engine.truth_used_round["Player"] == 1

    asyncio.run(run())


def test_event_and_time_conditions_reveal_after_commit(tmp_path: Path) -> None:
    """Time and scheduled-event conditions reveal when the clock reaches them."""

    async def run() -> None:
        engine, sender, _ = await build_truth_game(tmp_path, time_config=TIME_CONFIG, events=EVENTS)
        assert truth_question_ids(sender) == ["q_start"]

        await engine.process_payload("host", payload("action", action="等待"))
        await engine.process_payload("player", payload("action", action="等待"))
        await engine.wait_for_inference()
        assert truth_question_ids(sender) == ["q_start", "q_round"]

        await engine.process_payload("host", payload("action", action="继续等待"))
        await engine.process_payload("player", payload("action", action="继续等待"))
        await engine.wait_for_inference()
        assert engine.fired_events == {"午夜钟声"}
        assert truth_question_ids(sender) == ["q_start", "q_round", "q_event", "q_time"]

    asyncio.run(run())


def test_truths_feed_next_round_and_respect_feed_dm(tmp_path: Path) -> None:
    """Accepted truths enter the next request unless feed_dm is disabled."""

    async def run() -> None:
        engine, _, resolver = await build_truth_game(tmp_path)
        context_before = engine._combined_event_context(
            {"event_context": "", "truth_context": engine._truth_context()}
        )
        assert context_before == ""
        await engine.process_payload(
            "player", payload("truth_submit", question_id="q_start", answer="灯塔守夜人看见了船。")
        )
        engine._refresh_truth_reveals_locked()
        context_after = engine._combined_event_context(
            {"event_context": "", "truth_context": engine._truth_context()}
        )
        assert "灯塔守夜人看见了船。" in context_after
        assert "Player 的可能真相" in context_after

        await engine.process_payload("host", payload("action", action="前往灯塔"))
        await engine.process_payload("player", payload("action", action="检查灯油"))
        await engine.wait_for_inference()
        assert "灯塔守夜人看见了船。" in resolver.event_context

        disabled, _, disabled_resolver = await build_truth_game(
            tmp_path / "disabled",
            plugins=plugin_config(feed_dm=False),
        )
        await disabled.process_payload(
            "player", payload("truth_submit", question_id="q_start", answer="礁石附近有暗流。")
        )
        await disabled.process_payload("host", payload("action", action="驾船"))
        await disabled.process_payload("player", payload("action", action="瞭望"))
        await disabled.wait_for_inference()
        assert "礁石附近有暗流。" not in disabled_resolver.event_context

    asyncio.run(run())


def test_spectator_cannot_submit(tmp_path: Path) -> None:
    """A host without a character can view but not submit truths."""

    async def run() -> None:
        engine, sender, _ = await build_truth_game(tmp_path, host_character="")
        await engine.process_payload(
            "host", payload("truth_submit", question_id="q_start", answer="这是房主的答案。")
        )
        assert sender.events_of_type("error")[-1].payload["msg"] == "请先选择角色后再提交。"

    asyncio.run(run())


def test_truth_state_persists_across_restore(tmp_path: Path) -> None:
    """Revealed questions, answers and per-round usage survive a restart."""

    async def run() -> None:
        engine, _, _ = await build_truth_game(tmp_path)
        await engine.process_payload(
            "player", payload("truth_submit", question_id="q_start", answer="信是伪造的。")
        )
        data = engine.to_persistent_dict()
        assert data["plugins"][0]["type"] == "possible_truths"
        assert data["truth_revealed"] == ["q_start"]
        assert data["truth_used_round"] == {"Player": 1}

        restored = restore_engine(data, FakeSender(), FakeResolver)
        assert restored.state is GameState.ACTIVE_TURN
        assert restored.plugins[0].type == "possible_truths"
        assert restored.truth_revealed == {"q_start"}
        assert restored.truth_answers == {
            "q_start": [{"character": "Player", "text": "信是伪造的。"}]
        }
        assert restored.truth_used_round == {"Player": 1}
        assert restored._truths_plugin() is not None

    asyncio.run(run())


def test_action_answer_is_auto_collected_verbatim(tmp_path: Path) -> None:
    """A player answering a revealed question in their action is recorded verbatim."""

    async def run() -> None:
        engine, sender, resolver = await build_truth_game(tmp_path)
        resolver.truth_findings = [
            [ActionTruthFinding(question_id="q_start", character="Player", quote="管家在隐瞒时间")]
        ]
        await engine.process_payload("host", payload("action", action="盘问管家"))
        await engine.process_payload(
            "player", payload("action", action="我觉得管家在隐瞒时间——他昨晚根本不在账房。")
        )
        await engine.wait_for_inference()

        assert engine.truth_answers["q_start"] == [
            {"character": "Player", "text": "管家在隐瞒时间"}
        ]
        auto = [
            event
            for event in sender.events_of_type("truth_answer")
            if event.payload.get("source") == "action"
        ]
        assert auto[-1].payload == {
            "question_id": "q_start",
            "character": "Player",
            "text": "管家在隐瞒时间",
            "round": 1,
            "source": "action",
        }
        assert resolver.truth_extractions[0][0] == [("q_start", "谁在说谎？")]
        assert "管家在隐瞒时间" in resolver.truth_extractions[0][1]["Player"]
        assert "适时让相关人物" in resolver.event_context

    asyncio.run(run())


def test_paraphrased_action_answer_is_rejected(tmp_path: Path) -> None:
    """Quotes that are not verbatim in the action are discarded."""

    async def run() -> None:
        engine, sender, resolver = await build_truth_game(tmp_path)
        resolver.truth_findings = [
            [ActionTruthFinding(question_id="q_start", character="Player", quote="管家做了亏心事")]
        ]
        await engine.process_payload("host", payload("action", action="盘问管家"))
        await engine.process_payload("player", payload("action", action="管家看起来有点紧张。"))
        await engine.wait_for_inference()
        assert engine.truth_answers == {}
        assert not [
            event
            for event in sender.events_of_type("truth_answer")
            if event.payload.get("source") == "action"
        ]

    asyncio.run(run())


def test_action_answers_drop_unknown_targets_and_duplicates(tmp_path: Path) -> None:
    """Unknown characters/questions are dropped and repeated answers recorded once."""

    async def run() -> None:
        engine, sender, resolver = await build_truth_game(tmp_path)
        resolver.truth_findings = [
            [
                ActionTruthFinding(
                    question_id="q_start", character="Player", quote="管家在隐瞒时间"
                ),
                ActionTruthFinding(question_id="q_start", character="Ghost", quote="无人"),
                ActionTruthFinding(
                    question_id="q_nope", character="Player", quote="管家在隐瞒时间"
                ),
            ],
            [ActionTruthFinding(question_id="q_start", character="Player", quote="管家在隐瞒时间")],
        ]
        await engine.process_payload("host", payload("action", action="盘问管家"))
        await engine.process_payload("player", payload("action", action="管家在隐瞒时间。"))
        await engine.wait_for_inference()
        assert engine.truth_answers["q_start"] == [
            {"character": "Player", "text": "管家在隐瞒时间"}
        ]

        await engine.process_payload("host", payload("action", action="继续盘问"))
        await engine.process_payload("player", payload("action", action="管家在隐瞒时间。"))
        await engine.wait_for_inference()
        assert engine.truth_answers["q_start"] == [
            {"character": "Player", "text": "管家在隐瞒时间"}
        ]
        auto = [
            event
            for event in sender.events_of_type("truth_answer")
            if event.payload.get("source") == "action"
        ]
        assert len(auto) == 1
        assert all(
            event.payload["character"] != "Ghost" for event in sender.events_of_type("truth_answer")
        )

    asyncio.run(run())

"""S1 input layer: proposal/response obligations, duration hints and review verdicts."""

import asyncio
from pathlib import Path

from core.schemas import ActionReview
from logic.engine import GameEngine, restore_engine
from logic.transcript import GameTranscript
from test_engine import FakeResolver, FakeSender, payload

TIME_RULES = [
    {
        "activity": "破解机关或开锁",
        "keys": ["开锁", "撬锁", "机关"],
        "minutes_min": 5,
        "minutes_max": 20,
    }
]


async def build_game(
    tmp_path: Path, *, time_rules: list[dict[str, object]] | None = None
) -> tuple[GameEngine, FakeSender, FakeResolver]:
    """Start a deterministic two-character game."""
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
            time_rules=time_rules,
        ),
    )
    await engine.wait_for_inference()
    await engine.player_join("player", {"name": "Player", "character": "Player"})
    await engine.process_payload("host", payload("start_game"))
    await engine.wait_for_inference()
    return engine, sender, resolver


def test_vague_and_duration_specified_actions_are_rejected(tmp_path: Path) -> None:
    """Outcome claims and player-specified durations are rejected with guidance."""

    async def run() -> None:
        engine, sender, resolver = await build_game(tmp_path)
        resolver.review_results = [ActionReview(valid=False, reason="请改为『向队友提议怎么做』。")]
        await engine.process_payload("host", payload("action", action="让队友配合我一起搜查"))
        assert sender.events_of_type("error")[-1].payload["msg"] == "请改为『向队友提议怎么做』。"
        assert engine.round_buffer == {}

        resolver.review_results = [ActionReview(valid=False, reason="请不要在输入里指定耗时。")]
        await engine.process_payload("host", payload("action", action="花 1 分钟撬开锁"))
        assert sender.events_of_type("error")[-1].payload["msg"] == "请不要在输入里指定耗时。"
        assert engine.round_buffer == {}

    asyncio.run(run())


def test_proposal_is_registered_and_duration_zero(tmp_path: Path) -> None:
    """A proposal registers a pending obligation and takes zero in-game time."""

    async def run() -> None:
        engine, _, resolver = await build_game(tmp_path)
        resolver.review_results = [
            ActionReview(valid=True, kind="proposal", proposal_to="Player", duration_minutes=30)
        ]
        await engine.process_payload("host", payload("action", action="向灰刺提议一起搜查"))
        assert engine.pending_proposals == [
            {
                "from": "Host",
                "to": "Player",
                "content": "向灰刺提议一起搜查",
                "resolved": False,
                "response": None,
                "round": 1,
            }
        ]
        assert engine.round_durations["host"] == 0

    asyncio.run(run())


def test_proposal_targets_are_validated(tmp_path: Path) -> None:
    """Proposals must target an existing teammate and not repeat an open one."""

    async def run() -> None:
        engine, sender, resolver = await build_game(tmp_path)
        resolver.review_results = [ActionReview(valid=True, kind="proposal", proposal_to="Nobody")]
        await engine.process_payload("host", payload("action", action="向陌生人提议"))
        assert (
            sender.events_of_type("error")[-1].payload["msg"]
            == "提议的对象必须是队伍中的其他角色。"
        )

        engine.pending_proposals = [
            {
                "from": "Host",
                "to": "Player",
                "content": "先前的提议",
                "resolved": False,
                "response": None,
                "round": 1,
            }
        ]
        resolver.review_results = [ActionReview(valid=True, kind="proposal", proposal_to="Player")]
        await engine.process_payload("host", payload("action", action="再次向灰刺提议"))
        assert (
            sender.events_of_type("error")[-1].payload["msg"]
            == "Player 还有未回应的提议，请等待其先处理。"
        )

    asyncio.run(run())


def test_response_obligation_is_enforced_and_cleared(tmp_path: Path) -> None:
    """An open proposal must be answered before an unrelated action is accepted."""

    async def run() -> None:
        engine, sender, resolver = await build_game(tmp_path)
        resolver.review_results = [ActionReview(valid=True, kind="proposal", proposal_to="Player")]
        await engine.process_payload("host", payload("action", action="向灰刺提议一起搜查"))
        assert engine.active_player_id == "player"

        resolver.review_results = [ActionReview(valid=True)]
        await engine.process_payload("player", payload("action", action="我检查门锁"))
        assert sender.events_of_type("error")[-1].payload["msg"] == (
            "请先回应 Host 的提议（同意 / 无视 / 拒绝）。"
        )
        assert engine.pending_proposals[0]["resolved"] is False

        resolver.review_results = [
            ActionReview(valid=True, kind="response", response_to="Host", response="agree")
        ]
        await engine.process_payload("player", payload("action", action="我同意，跟他一起搜查"))
        assert engine.pending_proposals == []
        await engine.wait_for_inference()

        resolver.review_results = [ActionReview(valid=True, kind="response", response_to="Ghost")]
        await engine.process_payload("host", payload("action", action="回应幽灵"))
        assert sender.events_of_type("error")[-1].payload["msg"] == "该提议的发起者不在队伍中。"

    asyncio.run(run())


def test_duration_is_clamped_to_matched_rule(tmp_path: Path) -> None:
    """A rule-matched duration is clamped into the rule's range."""

    async def run() -> None:
        engine, _, resolver = await build_game(tmp_path, time_rules=TIME_RULES)
        resolver.review_results = [
            ActionReview(valid=True, matched_rule="破解机关或开锁", duration_minutes=60)
        ]
        await engine.process_payload("host", payload("action", action="撬开锁"))
        assert engine.round_durations["host"] == 20

    asyncio.run(run())


def test_unmatched_duration_uses_the_estimate(tmp_path: Path) -> None:
    """Without a matching rule the AI estimate is used as-is."""

    async def run() -> None:
        engine, _, resolver = await build_game(tmp_path)
        resolver.review_results = [ActionReview(valid=True, duration_minutes=12)]
        await engine.process_payload("host", payload("action", action="观察四周"))
        assert engine.round_durations["host"] == 12

    asyncio.run(run())


def test_review_outage_fails_open(tmp_path: Path) -> None:
    """A review backend outage must not block the action."""

    async def run() -> None:
        engine, _, resolver = await build_game(tmp_path)
        resolver.review_results = [RuntimeError("backend down")]
        await engine.process_payload("host", payload("action", action="守在门口观察"))
        assert engine.round_buffer == {"host": "守在门口观察"}

    asyncio.run(run())


def test_pending_proposals_survive_restore(tmp_path: Path) -> None:
    """Pending proposal obligations are persisted and rebuilt."""

    async def run() -> None:
        engine, _, resolver = await build_game(tmp_path)
        resolver.review_results = [ActionReview(valid=True, kind="proposal", proposal_to="Player")]
        await engine.process_payload("host", payload("action", action="向灰刺提议一起搜查"))
        data = engine.to_persistent_dict()
        assert data["pending_proposals"][0]["to"] == "Player"

        restored = restore_engine(data, FakeSender(), FakeResolver)
        assert restored.pending_proposals[0]["from"] == "Host"
        assert restored.pending_proposals[0]["resolved"] is False

    asyncio.run(run())


def test_proposal_target_name_is_case_insensitive(tmp_path: Path) -> None:
    """A mis-cased target name is mapped to the canonical roster spelling."""

    async def run() -> None:
        engine, _, resolver = await build_game(tmp_path)
        resolver.review_results = [ActionReview(valid=True, kind="proposal", proposal_to="player")]
        await engine.process_payload("host", payload("action", action="向灰刺提议"))
        assert engine.pending_proposals[0]["to"] == "Player"

    asyncio.run(run())


def test_disconnect_drops_involving_proposals(tmp_path: Path) -> None:
    """A disconnect resolves and clears any proposal involving that character."""

    async def run() -> None:
        engine, _, resolver = await build_game(tmp_path)
        resolver.review_results = [ActionReview(valid=True, kind="proposal", proposal_to="Player")]
        await engine.process_payload("host", payload("action", action="向灰刺提议"))
        assert engine.pending_proposals
        await engine.handle_disconnect("player")
        assert engine.pending_proposals == []

    asyncio.run(run())


def test_agreement_forms_a_plan_and_execution_completes_it(tmp_path: Path) -> None:
    """Accepting a proposal forms a shared plan; both executing it completes the plan."""

    async def run() -> None:
        engine, _, resolver = await build_game(tmp_path)
        resolver.review_results = [ActionReview(valid=True, kind="proposal", proposal_to="Player")]
        await engine.process_payload("host", payload("action", action="向灰刺提议一起搜查"))
        resolver.review_results = [
            ActionReview(valid=True, kind="response", response_to="Host", response="agree")
        ]
        await engine.process_payload("player", payload("action", action="我同意"))
        assert len(engine.agreed_plans) == 1
        plan = engine.agreed_plans[0]
        assert plan["participants"] == ["Host", "Player"]
        assert plan["status"] == "agreed"
        await engine.wait_for_inference()

        resolver.review_results = [ActionReview(valid=True, plan_id=plan["id"])]
        await engine.process_payload("host", payload("action", action="按约定围住门口"))
        assert plan["executed"] == ["Host"]
        assert plan["status"] == "agreed"

        resolver.review_results = [ActionReview(valid=True, plan_id=plan["id"])]
        await engine.process_payload("player", payload("action", action="按约定破门"))
        assert plan["status"] == "done"

    asyncio.run(run())


def test_refusal_forms_no_plan(tmp_path: Path) -> None:
    """Refusing a proposal ends it without creating a plan."""

    async def run() -> None:
        engine, _, resolver = await build_game(tmp_path)
        resolver.review_results = [ActionReview(valid=True, kind="proposal", proposal_to="Player")]
        await engine.process_payload("host", payload("action", action="向灰刺提议"))
        resolver.review_results = [
            ActionReview(valid=True, kind="response", response_to="Host", response="refuse")
        ]
        await engine.process_payload("player", payload("action", action="我拒绝"))
        assert engine.pending_proposals == []
        assert engine.agreed_plans == []

    asyncio.run(run())


def test_executing_unknown_plan_is_rejected(tmp_path: Path) -> None:
    """Executing a plan that does not exist is rejected."""

    async def run() -> None:
        engine, sender, resolver = await build_game(tmp_path)
        resolver.review_results = [ActionReview(valid=True, plan_id="plan-99")]
        await engine.process_payload("host", payload("action", action="按约定行动"))
        assert sender.events_of_type("error")[-1].payload["msg"] == "没有可执行的约定。"

    asyncio.run(run())


def test_departed_character_drops_agreed_plans(tmp_path: Path) -> None:
    """A shared plan is dropped when a participant leaves the game."""

    async def run() -> None:
        engine, _, resolver = await build_game(tmp_path)
        resolver.review_results = [ActionReview(valid=True, kind="proposal", proposal_to="Player")]
        await engine.process_payload("host", payload("action", action="向灰刺提议"))
        resolver.review_results = [
            ActionReview(valid=True, kind="response", response_to="Host", response="agree")
        ]
        await engine.process_payload("player", payload("action", action="我同意"))
        assert engine.agreed_plans
        await engine.handle_disconnect("player")
        assert engine.agreed_plans == []

    asyncio.run(run())

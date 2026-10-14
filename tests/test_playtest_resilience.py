"""Driver resilience: refusals, retries and the loops they can cause.

A game master can reject any action, and the engine re-sends a directive or a
window after every rejection. Without a ceiling the driver would keep calling
seat models forever without resolving anything, which is both a hang and a bill.
"""

from __future__ import annotations

import asyncio
import time
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from api.server import create_app
from core.schemas import ActionReview
from playtest.card import inspect_card
from playtest.agent import SeatAuthError
from playtest.runner import PlaytestRunner
from test_engine import FakeResolver
from test_playtest_runner import InProcessHub, build_config, round_card, write_card
from test_playtest_strict import strict_card

CONTENT_REJECTION = "行动描述不够具体，请写清角色具体做了什么。"
TIMING_REJECTION = "现在不是你的行动窗口，请等待。"
RUN_TIMEOUT_SECONDS = 45.0


class RejectingResolver(FakeResolver):
    """A fake DM that refuses one character's actions a fixed number of times."""

    def __init__(self, character: str = "甲", *, times: int = 1, reason: str = CONTENT_REJECTION):
        """Refuse ``times`` actions (use a large number for "forever")."""
        super().__init__()
        self.character = character
        self.times = times
        self.reason = reason
        self.rejections = 0

    async def review_action(self, character: str, action: str, **kwargs: Any) -> ActionReview:
        """Refuse this character's actions until the quota is used up."""
        del action, kwargs
        if character == self.character and self.rejections < self.times:
            self.rejections += 1
            return ActionReview(valid=False, reason=self.reason)
        return ActionReview(valid=True, reason="")


class SteppedResolver(FakeResolver):
    """A fake DM that gives each character its own action duration."""

    def __init__(self, durations: dict[str, int]) -> None:
        """Store the in-game minutes each character's action takes."""
        super().__init__()
        self.durations = durations

    async def review_action(self, character: str, action: str, **kwargs: Any) -> ActionReview:
        """Accept, with a per-character duration."""
        del action, kwargs
        return ActionReview(
            valid=True, reason="", duration_minutes=self.durations.get(character, 20)
        )


def play(
    tmp_path: Path,
    payload: Any,
    resolver: FakeResolver,
    *,
    limits: dict[str, Any] | None = None,
    brain_factory: Any = None,
) -> tuple[Any, float, PlaytestRunner]:
    """Run one game in-process against a specific fake DM."""
    card = inspect_card(write_card(tmp_path, payload))
    assert card.ok, card.findings
    config = build_config(
        run={"max_rounds": 2, "stall_seconds": 6, "seat_timeout_seconds": 5, **(limits or {})}
    )
    app = create_app(lambda: resolver)
    with TestClient(app) as client:
        hub = InProcessHub(client)
        runner = PlaytestRunner(
            config,
            card,
            transport_factory=hub.transport_factory,
            brain_factory=brain_factory,
        )
        try:
            started = time.monotonic()
            report = asyncio.run(asyncio.wait_for(runner.run(), RUN_TIMEOUT_SECONDS))
        finally:
            hub.close()
    return report, time.monotonic() - started, runner


def test_a_single_refusal_is_retried_and_the_run_continues(tmp_path: Path) -> None:
    """The normal path: one rejection, one retry, the round resolves."""
    resolver = RejectingResolver(times=1)

    report, _, _ = play(tmp_path, round_card(), resolver)

    assert resolver.rejections == 1
    assert report.rounds_completed == 2
    seat = next(item for item in report.seats if item["character"] == "甲")
    assert seat["retries"] == 1
    assert seat["failures"] == 1
    # A refusal the driver recovered from is not a run failure.
    assert report.errors == []
    assert report.rejections == [CONTENT_REJECTION]
    assert report.verdict != "failed"
    assert any(item["code"] == "action_rejected" for item in report.health["issues"])


def test_a_repeatedly_refused_seat_stops_hammering(tmp_path: Path) -> None:
    """A referee that refuses everything must not become an endless loop."""
    resolver = RejectingResolver(times=10_000)

    report, elapsed, _ = play(tmp_path, round_card(), resolver)

    assert resolver.rejections <= 8, resolver.rejections
    assert elapsed < 20, elapsed
    assert report.rounds_completed == 0
    assert any("停止为该座位提交" in item or "兜底行动" in item for item in report.warnings)
    assert len(report.warnings) < 10


def test_timing_refusals_are_bounded_as_well(tmp_path: Path) -> None:
    """Waiting for the next window must not spin either."""
    resolver = RejectingResolver(times=10_000, reason=TIMING_REJECTION)

    report, elapsed, _ = play(tmp_path, round_card(), resolver)

    assert resolver.rejections <= 8, resolver.rejections
    assert elapsed < 20, elapsed
    assert any("等下一回合" in item or "请等待" in item for item in report.warnings)


def test_the_submission_ceiling_is_per_round(tmp_path: Path) -> None:
    """A new round clears the ceiling, so a seat keeps playing after a bad one."""
    resolver = RejectingResolver(times=3)  # refuses the first round, accepts later

    report, _, runner = play(tmp_path, round_card(), resolver)

    seat = next(item for item in report.seats if item["character"] == "甲")
    assert runner is not None
    assert seat["actions"] >= 1, report.seats
    assert report.rounds_completed >= 1


def test_actions_are_attributed_to_the_boundary_that_resolves_them(tmp_path: Path) -> None:
    """Strict actions land in the round whose boundary actually resolved them."""
    resolver = SteppedResolver({"甲": 30, "乙": 60})

    report, _, _ = play(
        tmp_path,
        strict_card(),
        resolver,
        limits={"max_rounds": 2, "stall_seconds": 10},
    )

    assert report.rounds_completed == 2
    # Every action is reported under the boundary that actually resolved it.
    for record in report.rounds:
        assert set(record["actions"]) <= set(record["outcomes"]), record
    first = report.rounds[0]
    assert set(first["actions"]) == {"甲"}, first
    assert "乙" not in first["outcomes"], first
    assert any("乙" in record["actions"] for record in report.rounds), report.rounds


class BrokenCredentialsBrain:
    """A seat whose key the provider rejects."""

    def __init__(self) -> None:
        """Count how often the seat was asked."""
        self.calls = 0

    async def act(self, turn: Any) -> str:
        """Fail like a 401 from the provider."""
        del turn
        self.calls += 1
        raise SeatAuthError("座位 甲 的 API Key 被拒绝（HTTP 401）：Incorrect API key provided.")


def test_a_rejected_api_key_stops_the_run_instead_of_burning_calls(tmp_path: Path) -> None:
    """A 401 is not transient: stop early and say so."""
    brains: list[BrokenCredentialsBrain] = []

    def factory(plan: Any, character: Any) -> BrokenCredentialsBrain:
        del plan, character
        brain = BrokenCredentialsBrain()
        brains.append(brain)
        return brain

    report, elapsed, _ = play(
        tmp_path,
        round_card(),
        RejectingResolver(times=0),
        limits={"stall_seconds": 10},
        brain_factory=factory,
    )

    assert elapsed < 5, elapsed
    assert sum(brain.calls for brain in brains) <= 3, [brain.calls for brain in brains]
    assert report.rounds_completed == 0
    assert any("凭据被拒绝" in item for item in report.errors)
    assert "凭据被拒绝" in report.stop_reason
    assert report.health["verdict"] == "failed"


class PickyResolver(FakeResolver):
    """Refuses until it has seen enough genuinely different attempts.

    That is what a strict referee does to a driver that keeps repeating itself:
    the identical text is useless, a different concrete action gets through.
    """

    def __init__(self, accept_after: int = 4) -> None:
        """Accept once this many distinct action texts have been seen."""
        super().__init__()
        self.accept_after = accept_after
        self.refusals = 0
        self.seen: set[str] = set()

    async def review_action(self, character: str, action: str, **kwargs: Any) -> ActionReview:
        """Refuse a repeat or an early attempt; accept a fresh action later."""
        del character, kwargs
        self.seen.add(action)
        if len(self.seen) >= self.accept_after:
            return ActionReview(valid=True, reason="")
        self.refusals += 1
        return ActionReview(valid=False, reason="这只是意图，请写清具体做什么。")


def test_the_fallback_ladder_tries_a_different_action_each_time(tmp_path: Path) -> None:
    """Repeating one fallback text would just be refused again."""
    brains: list[Any] = []

    class VagueBrain:
        """A seat that only produces the vague intent a strict referee refuses."""

        async def act(self, turn: Any) -> str:
            del turn
            return "直接处理眼前最紧要的问题。"

    def factory(plan: Any, character: Any) -> Any:
        del plan, character
        brain = VagueBrain()
        brains.append(brain)
        return brain

    resolver = PickyResolver()

    report, _, runner = play(
        tmp_path,
        round_card(),
        resolver,
        limits={"max_rounds": 1, "stall_seconds": 10},
        brain_factory=factory,
    )

    assert report.rounds_completed == 1, report.stop_reason
    seat = next(item for item in report.seats if item["character"] == "甲")
    # The model repeated one vague intent; the ladder then tried different
    # concrete actions until one was accepted.
    assert seat["fallbacks"] >= 2, seat
    assert seat["actions"] >= 1
    refusals = [item for item in report.warnings if "兜底行动也被驳回" in item]
    assert len(refusals) >= 1, report.warnings
    texts = [item["message"] for item in runner.progress_events if item["kind"] == "action"]
    assert len(set(texts)) >= 2, texts


def test_a_paused_round_is_retried_by_the_host(tmp_path: Path) -> None:
    """The engine keeps the actions and asks the host to retry; so must we."""
    attempts: dict[str, int] = {"rounds": 0}

    class FlakyResolver(FakeResolver):
        """Fails the first resolution, then behaves."""

        async def generate_resolution(self, round_buffer, *args, **kwargs):
            attempts["rounds"] += 1
            if attempts["rounds"] == 1:
                raise RuntimeError("模拟 DM 结算失败")
            return await super().generate_resolution(round_buffer, *args, **kwargs)

    report, elapsed, runner = play(
        tmp_path,
        round_card(),
        FlakyResolver(),
        limits={"max_rounds": 1, "stall_seconds": 15},
    )

    assert report.rounds_completed == 1, report.stop_reason
    assert report.pauses, report.pauses
    assert any("重试" in item["message"] for item in runner.progress_events)
    assert report.health["verdict"] != "failed"
    assert any(item["code"] == "dm_round_paused" for item in report.health["issues"])
    assert elapsed < 30, elapsed


def test_progress_reports_what_each_seat_submitted(tmp_path: Path) -> None:
    """The live progress stream shows the action text, not just a notice."""
    report, _, runner = play(tmp_path, round_card(), RejectingResolver(times=0))

    events = runner.progress_events
    actions = [item for item in events if item["kind"] == "action"]
    assert len(actions) >= 2
    assert all("：" in item["message"] for item in actions)
    assert any("：" in item["message"] and len(item["message"]) > 10 for item in actions)

    round_lines = [item for item in events if item["kind"] == "round"]
    assert len(round_lines) == report.rounds_completed
    assert any(item["kind"] == "round-action" for item in events)
    # Every round line names the characters that acted in it.
    for record in report.rounds:
        for character in record["actions"]:
            assert any(
                item["kind"] == "round-action" and character in item["message"] for item in events
            )

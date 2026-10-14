"""Strict timeline mode: windows, snapshots, possible truths and the livelock guard."""

from __future__ import annotations

import asyncio
import json
from pathlib import Path
from typing import Any

from fastapi.testclient import TestClient

from api.server import create_app
from core.schemas import Character
from playtest import protocol as P
from playtest.agent import (
    ScriptedBrain,
    SeatError,
    SeatTurn,
    TruthRequest,
    fallback_action,
    stable_offset,
)
from playtest.card import inspect_card
from playtest.config import PlaytestConfig, SeatLLM, SeatPlan
from playtest.protocol import Event
from playtest.runner import Connection, PlaytestRunner, SeatRuntime
from playtest.world import PublicView
from test_engine import FakeResolver
from test_playtest_runner import InProcessHub, build_config, round_card, write_card

RUN_TIMEOUT_SECONDS = 60.0

MAP_STATE: dict[str, Any] = {
    "start_region": "hall",
    "regions": [
        {
            "id": "hall",
            "name": "门厅",
            "items": [
                {
                    "id": "key",
                    "name": "黄铜钥匙",
                    "summary": "桌上的钥匙",
                    "clue": True,
                    "key_item": False,
                    "carryable": True,
                    "investigated": True,
                    "detail": "匙齿上有一道新划痕。",
                }
            ],
            "reachable": [{"id": "study", "name": "书房"}],
            "locked": [{"id": "attic", "name": "阁楼"}],
        }
    ],
    "players": [{"name": "甲", "region": "hall", "in_transit_to": None, "carried": ["黄铜钥匙"]}],
}

CODEX_STATE: dict[str, Any] = {
    "items": [
        {
            "id": "key",
            "name": "黄铜钥匙",
            "summary": "桌上的钥匙",
            "clue": True,
            "key_item": False,
            "investigated": True,
            "origin": "门厅",
            "here": True,
        }
    ],
    "events": [{"name": "敲门声", "description": "有人在敲门。", "region": "", "public": True}],
}


class FakeTransport:
    """Records envelopes instead of talking to a server."""

    def __init__(self) -> None:
        """Start with nothing sent."""
        self.sent: list[dict[str, Any]] = []
        self.connected = True

    async def open(self) -> None:
        """Nothing to open."""
        return None

    async def send(self, envelope: dict[str, Any]) -> None:
        """Record one envelope."""
        self.sent.append(envelope)

    async def receive(self) -> dict[str, Any]:
        """Never used in unit tests."""
        raise AssertionError("receive() is not used by these tests")

    async def close(self) -> None:
        """Mark the transport closed."""
        self.connected = False


class RecordingBrain:
    """A scripted seat that remembers every briefing it was given."""

    def __init__(self, start_index: int = 0) -> None:
        """Wrap a scripted brain."""
        self.inner = ScriptedBrain(start_index=start_index)
        self.digests: list[str] = []
        self.truth_requests: list[TruthRequest] = []

    async def act(self, turn: SeatTurn) -> str:
        """Record the briefing, then answer like the scripted seat."""
        self.digests.append(turn.digest)
        return await self.inner.act(turn)

    async def answer_truth(self, request: TruthRequest) -> str:
        """Record the question, then answer like the scripted seat."""
        self.truth_requests.append(request)
        return await self.inner.answer_truth(request)


class TracingResolver(FakeResolver):
    """Fake DM that keeps its truth-referee calls visible to the test."""

    pass


def strict_card(**overrides: Any) -> dict[str, Any]:
    """Return a valid strict-mode card: regions, a clock, an event and a plugin."""
    payload: dict[str, Any] = {
        "scenario": "【剧本：时间轴跑测卡】\n\n雪夜，两个人被困在观测站。",
        "guidance": "真相：钥匙藏在花盆底下，谁先找到谁占上风。",
        "characters": [
            {"name": "甲", "description": "谨慎的锁匠", "personality": "话少，先看后动"},
            {"name": "乙", "description": "急躁的记者", "personality": "抢话，爱冒险"},
        ],
        "time_config": {
            "enabled": True,
            "start_day": 1,
            "start_minute": 600,
            "max_elapsed_minutes": 720,
            "default_elapsed_minutes": 20,
        },
        "time_rules": [
            {"activity": "搜索房间", "keys": ["搜索"], "minutes_min": 10, "minutes_max": 40}
        ],
        "regions": [
            {
                "id": "hall",
                "name": "门厅",
                "arrival_text": "风从门缝灌进来。",
                "items": ["key"],
                "links": [{"to": "study"}],
            },
            {"id": "study", "name": "书房", "items": [], "links": []},
        ],
        "items": [{"id": "key", "name": "黄铜钥匙", "region": "hall", "clue": True}],
        "events": [{"name": "敲门声", "day": 1, "minute": 700, "description": "有人在敲门。"}],
        "world": {"start_region": "hall"},
        "plugins": [
            {
                "type": "possible_truths",
                "enabled": True,
                "config": {
                    "questions": [
                        {"id": "q1", "text": "谁锁上了观测站的门？", "condition": "start"}
                    ]
                },
            }
        ],
    }
    payload.update(overrides)
    return payload


def play_strict(
    tmp_path: Path,
    payload: Any,
    *,
    config: PlaytestConfig | None = None,
    brain_factory: Any = None,
) -> tuple[Any, TracingResolver]:
    """Run one strict-mode game in-process and return the report and the DM."""
    card = inspect_card(write_card(tmp_path, payload))
    assert card.ok, card.findings
    resolver = TracingResolver()
    app = create_app(lambda: resolver)
    limits = config or build_config(run={"max_rounds": 2, "stall_seconds": 15})
    with TestClient(app) as client:
        hub = InProcessHub(client)
        runner = PlaytestRunner(
            limits,
            card,
            transport_factory=hub.transport_factory,
            brain_factory=brain_factory,
        )
        try:
            report = asyncio.run(asyncio.wait_for(runner.run(), RUN_TIMEOUT_SECONDS))
        finally:
            hub.close()
    return report, resolver


def bare_runner(
    tmp_path: Path, payload: Any, *, clock: Any, config: PlaytestConfig | None = None
) -> PlaytestRunner:
    """Build a runner with fake transports but no server, for unit tests."""
    card = inspect_card(write_card(tmp_path, payload))
    assert card.ok, card.findings
    limits = config or build_config(run={"max_rounds": 5, "stall_seconds": 5})
    return PlaytestRunner(
        limits,
        card,
        transport_factory=lambda client_id: FakeTransport(),
        clock=clock,
    )


def fake_connection(
    client_id: str = "c1", character: str = "甲"
) -> tuple[Connection, FakeTransport]:
    """Build one connection with a recording transport."""
    transport = FakeTransport()
    connection = Connection(
        client_id=client_id,
        name=f"{character}·AI",
        transport=transport,
        view=PublicView(client_id=client_id, name=f"{character}·AI", character=character),
        map_event=asyncio.Event(),
        codex_event=asyncio.Event(),
        truth_event=asyncio.Event(),
    )
    return connection, transport


def test_a_strict_card_plays_completed_boundaries(tmp_path: Path) -> None:
    """Windows, action durations and completion boundaries all work end to end."""
    report, _ = play_strict(tmp_path, strict_card())

    assert report.schedule == "strict"
    assert report.rounds_completed == 2
    assert "回合上限" in report.stop_reason
    assert report.errors == []
    assert len(report.rounds) == 2
    assert all(record["narrative"] for record in report.rounds)
    assert all(set(record["actions"]) == {"甲", "乙"} for record in report.rounds)
    times = [record["game_time"] for record in report.rounds]
    assert all(times), times
    assert times[0] != times[1], times
    for seat in report.seats:
        assert seat["actions"] >= 1
        assert seat["failures"] == 0


def test_seats_see_the_map_codex_and_truth_in_their_briefing(tmp_path: Path) -> None:
    """Strict-mode seats ask for the shared snapshots before deciding."""
    brains: list[RecordingBrain] = []

    def factory(plan: SeatPlan, character: Character) -> RecordingBrain:
        brain = RecordingBrain(start_index=stable_offset(plan.character, 4))
        brains.append(brain)
        return brain

    report, _ = play_strict(tmp_path, strict_card(), brain_factory=factory)

    assert report.rounds_completed == 2
    digests = [digest for brain in brains for digest in brain.digests]
    assert digests
    assert any("【地图】" in digest for digest in digests), digests[0]
    assert any("【图鉴】" in digest for digest in digests), digests[0]


def test_coverage_and_health_grade_a_strict_run(tmp_path: Path) -> None:
    """A strict run reports what it covered and why it stopped."""
    report, _ = play_strict(
        tmp_path,
        strict_card(
            events=[{"name": "敲门声", "day": 1, "minute": 620, "description": "有人在敲门。"}]
        ),
        config=build_config(run={"max_rounds": 2, "stall_seconds": 15}),
    )

    coverage = report.coverage
    assert coverage["regions"]["visited"] >= 1
    assert coverage["items"]["total"] == 1
    assert coverage["events"]["fired"] >= 1
    assert coverage["events"]["overdue"] == []
    assert coverage["time"]["elapsed_minutes"] > 0
    assert [row["character"] for row in coverage["participation"]] == ["甲", "乙"]

    assert report.health["verdict"] == "unfinished"
    assert report.exit_code == 3
    assert "地区" in report.to_markdown()
    assert "## 覆盖度" in report.to_markdown()
    assert "## 健康检查" in report.to_markdown()


def test_possible_truths_are_answered_by_the_seats(tmp_path: Path) -> None:
    """A revealed question reaches a seat and its claim reaches the DM."""
    report, resolver = play_strict(
        tmp_path, strict_card(), config=build_config(run={"max_rounds": 1, "stall_seconds": 15})
    )

    assert report.rounds_completed == 1
    assert any(seat["truth"] >= 1 for seat in report.seats), report.seats
    assert resolver.truth_calls, "the DM never judged a truth claim"
    assert report.errors == []
    truths = report.coverage["truths"]
    assert truths["enabled"] is True
    assert truths["revealed"] >= 1
    assert truths["answered"] >= 1
    assert truths["claims"], truths


def test_the_livelock_guard_switches_to_forced_actions(tmp_path: Path) -> None:
    """Repeated windows that never move the clock stop the seats asking models."""
    fake_time = [1_000.0]
    runner = bare_runner(tmp_path, round_card(), clock=lambda: fake_time[0])
    connection, _ = fake_connection()
    window = Event(
        type=P.TURN_WINDOW,
        payload={
            "round_number": 1,
            "game_time": "Day 1 08:30",
            "active_player_ids": [connection.client_id],
            "waiting": [],
        },
    )

    async def run() -> tuple[bool, bool, list[str]]:
        runner.started_monotonic = 1_000.0
        runner.last_progress_at = 1_000.0
        runner._apply(connection, window)
        before = runner.force_action
        fake_time[0] += 6.0  # past the 5s stall budget while the clock stands still
        runner._apply(connection, window)
        forced = runner.force_action
        advanced = Event(
            type=P.TURN_WINDOW,
            payload={**window.payload, "game_time": "Day 1 08:50"},
        )
        runner._apply(connection, advanced)
        return before, forced, list(runner.warnings)

    before, forced, warnings = asyncio.run(run())

    assert before is False
    assert forced is True
    assert runner.force_action is False  # in-game time moved, so trust returns
    assert any("强制" in item for item in warnings)


def test_a_forced_turn_skips_the_model_and_submits_an_action(tmp_path: Path) -> None:
    """When the guard is on, the seat submits deterministically without a call."""
    runner = bare_runner(tmp_path, round_card(), clock=lambda: 0.0)
    connection, transport = fake_connection()
    character = Character(name="甲")

    class Boom:
        """A brain that must never be asked."""

        def __init__(self) -> None:
            """Count calls so the test can assert zero."""
            self.calls = 0

        async def act(self, turn: SeatTurn) -> str:
            """Fail loudly if the runner consults the model."""
            del turn
            self.calls += 1
            raise SeatError("model must not be called while forced")

    brain = Boom()
    seat = SeatRuntime(
        character=character,
        style="explore",
        plan=SeatPlan(character="甲", style="explore", llm=SeatLLM()),
        brain=brain,
        connection=connection,
    )
    runner.seats.append(seat)
    runner.seats_by_client[connection.client_id] = seat

    async def run() -> None:
        runner.force_action = True
        task = asyncio.create_task(runner._seat_turn(seat, 1))
        await asyncio.sleep(0.05)
        assert transport.sent[-1]["event_type"] == "action"
        runner._resolve_action(connection, ("accepted", ""))
        await task

    asyncio.run(run())

    assert brain.calls == 0
    assert seat.forced_actions == 1
    assert transport.sent[-1]["data"]["action"] == fallback_action(character, 1)


def test_map_and_codex_briefs_follow_the_server_payload() -> None:
    """The briefing mirrors visited regions, gated paths and party positions."""
    view = PublicView(client_id="c1", character="甲")
    view.apply(Event(type=P.MAP_STATE, payload=MAP_STATE))
    view.apply(Event(type=P.CODEX_STATE, payload=CODEX_STATE))

    map_text = view.map_brief()
    assert "门厅" in map_text
    assert "黄铜钥匙（已查看）" in map_text
    assert "可前往 书房" in map_text
    assert "受阻 阁楼" in map_text
    assert "甲在门厅" in map_text
    assert "携带 黄铜钥匙" in map_text

    codex_text = view.codex_brief()
    assert "黄铜钥匙（线索）［已查看］" in codex_text
    assert "敲门声" in codex_text


def test_the_digest_shows_other_players_declared_actions() -> None:
    """Seats must be able to react to what the others declared this round."""
    view = PublicView(client_id="c1", character="甲", title="卡", game_time="Day 1 10:00")
    view.apply(
        Event(
            type=P.ACTION_ECHO,
            payload={"round_number": 2, "player_name": "乙", "action": "我走向门口的登记簿。"},
        )
    )
    view.apply(
        Event(
            type=P.ACTION_ECHO,
            payload={"round_number": 2, "player_name": "甲", "action": "我先看炉子。"},
        )
    )

    digest = view.digest()

    assert "【本轮其他人已经声明的行动" in digest
    assert "乙：我走向门口的登记簿。" in digest
    assert "甲：我先看炉子。" not in digest  # own action is not "other players"
    assert view.declared_actions() == {"乙": "我走向门口的登记簿。"}


def test_the_digest_surfaces_proposals_and_busy_players() -> None:
    """Pending proposals and in-flight actions are part of a player's picture."""
    view = PublicView(client_id="c1", character="甲", title="卡")
    view.apply(
        Event(
            type=P.AUTH_OK,
            payload={
                "name": "甲",
                "character": "甲",
                "pending_proposals": [{"from": "乙", "content": "一起去值班室看看？"}],
                "agreed_plans": [{"content": "先检查大堂再上楼"}],
            },
        )
    )
    view.apply(
        Event(
            type=P.TURN_WINDOW,
            payload={
                "round_number": 3,
                "game_time": "Day 1 10:30",
                "active_player_ids": ["c1"],
                "waiting": [
                    {
                        "player_id": "c2",
                        "name": "乙",
                        "action": "我翻找抽屉。",
                        "ends_at_label": "Day 1 11:00",
                    }
                ],
            },
        )
    )

    digest = view.digest()

    assert "【等你回应的提议】" in digest
    assert "乙：一起去值班室看看？" in digest
    assert "【已说好的计划】" in digest
    assert "【还在进行中】" in digest
    assert "乙：我翻找抽屉。" in digest  # the in-flight action text is shown


def test_the_digest_stays_bounded() -> None:
    """A long game must not send an ever-growing wall of prose."""
    view = PublicView(client_id="c1", character="甲", title="长卡", opening="开" * 2000)
    for number in range(1, 12):
        view.apply(
            Event(
                type=P.STATE_UPDATE,
                payload={
                    "round_number": number,
                    "game_time": f"Day 1 {10 + number}:00",
                    "global_narrative": f"第{number}回合的叙述。" * 60,
                    "player_resolutions": {"甲": "结果。" * 60, "乙": "别人的结果。" * 60},
                },
            )
        )

    digest = view.digest()

    assert len(digest) <= 4200, len(digest)
    assert "第11回合" in digest  # the newest round always survives
    assert "第1回合" not in digest  # the oldest is dropped first


def test_the_digest_includes_every_player_visible_section() -> None:
    """A seat briefing carries the map, the codex and the revealed questions."""
    view = PublicView(client_id="c1", character="甲", title="时间轴卡", opening="雪夜。")
    view.apply(Event(type=P.MAP_STATE, payload=MAP_STATE))
    view.apply(Event(type=P.CODEX_STATE, payload=CODEX_STATE))
    view.apply(
        Event(
            type=P.TRUTH_QUESTION,
            payload={"id": "q1", "text": "谁锁上了观测站的门？"},
        )
    )
    view.apply(
        Event(
            type=P.TRUTH_ANSWER,
            payload={"question_id": "q1", "character": "乙", "text": "我锁的，我怕有人进来。"},
        )
    )

    digest = view.digest()

    assert "【地图】" in digest
    assert "【图鉴】" in digest
    # The seat must be told that the surrounding prose is the DM's voice.
    assert "不要模仿这种叙述口吻" in digest
    assert "【可能的真相】" in digest
    assert "谁锁上了观测站的门？" in digest
    assert "乙：我锁的" in digest
    json.dumps(digest, ensure_ascii=False)

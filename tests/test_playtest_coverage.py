"""Coverage metrics and health grading for a playtest run."""

from __future__ import annotations

import json
from typing import Any

import pytest

from logic.lobby import parse_scenario_setup
from playtest.coverage import (
    build_coverage,
    build_health,
    event_coverage,
    item_coverage,
    lorebook_coverage,
    narrative_similarity,
    parse_game_time,
    participation,
    public_text,
    region_coverage,
    summarize_coverage,
    time_coverage,
    truth_coverage,
)

CARD: dict[str, Any] = {
    "scenario": "【剧本：覆盖度卡】\n\n雪夜，两个人被困。",
    "guidance": "真相：钥匙在花盆底下。",
    "characters": [{"name": "甲", "description": "锁匠"}, {"name": "乙", "description": "记者"}],
    "time_config": {
        "enabled": True,
        "start_day": 1,
        "start_minute": 600,
        "max_elapsed_minutes": 720,
        "default_elapsed_minutes": 20,
    },
    "regions": [
        {"id": "hall", "name": "门厅", "items": ["key"], "links": [{"to": "study"}]},
        {"id": "study", "name": "书房", "items": [], "links": []},
        {"id": "attic", "name": "阁楼", "items": [], "links": []},
    ],
    "items": [
        {"id": "key", "name": "黄铜钥匙", "region": "hall", "clue": True},
        {"id": "lamp", "name": "煤油灯", "region": "study"},
        {"id": "note", "name": "字条", "region": "study", "key_item": True},
    ],
    "events": [
        {"name": "敲门声", "day": 1, "minute": 700, "description": "有人在敲门。"},
        {"name": "断电", "day": 2, "minute": 120, "description": "灯灭了。"},
    ],
    "lorebook": [
        {"title": "观象站", "keys": ["观测站", "气象站"], "content": "旧气象站。"},
        {"title": "北方铁路", "keys": ["铁路"], "content": "山下的铁路。"},
    ],
    "world": {"start_region": "hall"},
    "plugins": [
        {
            "type": "possible_truths",
            "enabled": True,
            "config": {
                "questions": [
                    {"id": "q1", "text": "谁锁了门？", "condition": "start"},
                    {
                        "id": "q2",
                        "text": "谁剪了电话线？",
                        "condition": "event",
                        "event_name": "断电",
                    },
                ]
            },
        }
    ],
}

MAP_STATE: dict[str, Any] = {
    "start_region": "hall",
    "regions": [
        {
            "id": "hall",
            "name": "门厅",
            "items": [{"id": "key", "name": "黄铜钥匙", "investigated": True}],
            "reachable": [{"id": "study", "name": "书房"}],
            "locked": [],
        }
    ],
    "players": [{"name": "甲", "region": "hall", "carried": ["黄铜钥匙"]}],
}

CODEX_STATE: dict[str, Any] = {
    "items": [
        {"id": "key", "name": "黄铜钥匙", "clue": True, "investigated": True},
        {"id": "lamp", "name": "煤油灯", "clue": False, "investigated": False},
    ],
    "events": [{"name": "敲门声", "public": True}],
}

ROUNDS: list[dict[str, Any]] = [
    {
        "number": 1,
        "game_time": "Day 1 10:20",
        "actions": {"甲": "甲检查门锁。", "乙": "乙翻找观测站的抽屉。"},
        "narrative": "甲检查了门锁，乙在抽屉里找到了煤油灯。",
        "outcomes": {"甲": "门锁完好。", "乙": "煤油灯还有半壶油。"},
    },
    {
        "number": 2,
        "game_time": "Day 1 10:40",
        "actions": {"甲": "甲试着撬开阁楼的活门。", "乙": "乙记录下门厅的细节。"},
        "narrative": "甲试着撬开阁楼的活门，但没有成功。",
        "outcomes": {"甲": "活门纹丝不动。", "乙": "乙把细节写进本子。"},
    },
]


def setup():
    """Return the parsed card setup used by the coverage tests."""
    return parse_scenario_setup(CARD)


def test_parse_game_time_reads_server_labels() -> None:
    """Server clock labels convert to absolute minutes, day 1 starting at 1440."""
    assert parse_game_time("Day 1 00:00") == 1440
    assert parse_game_time("Day 1 10:20") == 1440 + 620
    assert parse_game_time("Day 2 00:00") == 2880
    assert parse_game_time("") is None
    assert parse_game_time("10:20") is None
    assert parse_game_time("Day x 10:20") is None


def test_region_coverage_counts_visited_against_the_card() -> None:
    """Visited regions come from the shared map, unvisited ones are listed."""
    coverage = region_coverage(setup(), MAP_STATE)

    assert coverage["total"] == 3
    assert coverage["visited"] == 1
    assert coverage["visited_names"] == ["门厅"]
    assert coverage["unvisited_names"] == ["书房", "阁楼"]


def test_item_coverage_separates_discovery_investigation_and_carrying() -> None:
    """Items are counted from the codex, with carrying read from the map."""
    coverage = item_coverage(setup(), MAP_STATE, CODEX_STATE)

    assert coverage["total"] == 3
    assert coverage["discovered"] == 2
    assert coverage["investigated"] == 1
    assert coverage["carried_names"] == ["黄铜钥匙"]
    assert coverage["clue_total"] == 2  # the clue key and the key item note
    assert coverage["clue_discovered"] == 1
    assert coverage["clue_investigated"] == 1


def test_event_coverage_flags_overdue_clock_events() -> None:
    """A clock event whose time has passed without firing is a card bug signal."""
    coverage = event_coverage(setup(), CODEX_STATE, parse_game_time("Day 1 10:40"))

    assert coverage["total"] == 2
    assert coverage["fired"] == 1
    assert coverage["fired_names"] == ["敲门声"]
    assert coverage["overdue"] == []

    later = event_coverage(setup(), CODEX_STATE, parse_game_time("Day 2 06:00"))
    assert later["overdue"] == ["断电"]


def test_lorebook_coverage_is_keyword_based_and_labelled() -> None:
    """World-book hits are approximated from the public text."""
    text = public_text(ROUNDS)

    coverage = lorebook_coverage(setup(), text)

    assert coverage["method"] == "keyword"
    assert coverage["total"] == 2
    assert coverage["hit_titles"] == ["观象站"]
    assert coverage["hit"] == 1


def test_truth_coverage_counts_revealed_and_answered() -> None:
    """Revealed questions with accepted claims count as answered."""
    revealed = [
        {"id": "q1", "text": "谁锁了门？", "answers": [{"character": "甲", "text": "我锁的。"}]},
        {"id": "q2", "text": "谁剪了电话线？", "answers": []},
    ]

    coverage = truth_coverage(setup(), revealed)

    assert coverage["enabled"] is True
    assert coverage["total"] == 2
    assert coverage["revealed"] == 2
    assert coverage["answered"] == 1
    assert coverage["claims"][0]["character"] == "甲"


def test_time_coverage_reports_clock_progress() -> None:
    """Clock progress is measured between the first and last boundary."""
    coverage = time_coverage(ROUNDS)

    assert coverage["start"] == "Day 1 10:20"
    assert coverage["end"] == "Day 1 10:40"
    assert coverage["elapsed_minutes"] == 20
    assert coverage["boundaries"] == 2


def test_participation_reports_actions_mentions_and_gaps() -> None:
    """Each character gets action rounds, mentions and its longest gap."""
    rows = {
        row["character"]: row
        for row in participation(
            setup(),
            ROUNDS
            + [
                {
                    "number": 3,
                    "game_time": "Day 1 11:00",
                    "actions": {"乙": "乙独自下楼。"},
                    "narrative": "乙独自下楼，甲留在门厅。",
                    "outcomes": {"乙": "楼梯很滑。"},
                }
            ],
        )
    }

    assert rows["甲"]["action_rounds"] == 2
    assert rows["甲"]["longest_gap"] == 1
    assert rows["甲"]["mentioned_rounds"] == 3
    assert rows["乙"]["action_rounds"] == 3
    assert rows["乙"]["longest_gap"] == 0


def test_narrative_similarity_flags_repeats() -> None:
    """Near-identical consecutive narratives are detected."""
    repeated = [
        {"number": 1, "narrative": "甲推了推门，门没有开。乙站在旁边看着。"},
        {"number": 2, "narrative": "甲推了推门，门没有开。乙站在旁边看着。"},
    ]

    pairs = narrative_similarity(repeated)

    assert pairs[0]["similarity"] == pytest.approx(1.0)


def test_build_coverage_assembles_every_section() -> None:
    """The coverage section is complete and JSON-serializable."""
    coverage = build_coverage(
        setup=setup(),
        rounds=ROUNDS,
        map_state=MAP_STATE,
        codex_state=CODEX_STATE,
        revealed_truths=[{"id": "q1", "text": "谁锁了门？", "answers": []}],
    )

    assert set(coverage) == {
        "regions",
        "items",
        "events",
        "lorebook",
        "truths",
        "time",
        "participation",
    }
    assert "地区 1/3" in summarize_coverage(coverage)
    json.dumps(coverage, ensure_ascii=False)


def test_health_grades_a_clean_but_unfinished_run() -> None:
    """A run that ran out of budget is unfinished, not failed."""
    coverage = build_coverage(
        setup=setup(),
        rounds=ROUNDS,
        map_state=MAP_STATE,
        codex_state=CODEX_STATE,
        revealed_truths=[
            {"id": "q1", "text": "谁锁了门？", "answers": [{"character": "甲", "text": "我锁的。"}]}
        ],
    )
    seats = [
        {
            "character": "甲",
            "failures": 0,
            "retries": 0,
            "fallbacks": 0,
            "forced": 0,
        }
    ]

    health = build_health(
        rounds=ROUNDS,
        seats=seats,
        warnings=[],
        errors=[],
        stop_reason="达到回合上限（2 回合）",
        coverage=coverage,
    )

    assert health["verdict"] == "unfinished"
    assert health["issues"] == []


def test_health_fails_on_lost_host_or_server_errors() -> None:
    """Fatal conditions grade the run as failed."""
    coverage = {"events": {"overdue": []}, "truths": {}}

    lost = build_health(
        rounds=ROUNDS,
        seats=[],
        warnings=[],
        errors=[],
        stop_reason="主机连接中断：会话已结束",
        coverage=coverage,
        host_lost=True,
    )
    assert lost["verdict"] == "failed"
    assert any(issue["code"] == "host_lost" for issue in lost["issues"])

    errored = build_health(
        rounds=ROUNDS,
        seats=[],
        warnings=[],
        errors=["还没轮到你。"],
        stop_reason="游戏已结束。",
        coverage=coverage,
    )
    assert errored["verdict"] == "failed"
    assert any(issue["code"] == "server_errors" for issue in errored["issues"])


def test_truth_coverage_flags_questions_the_engine_never_revealed() -> None:
    """A satisfied reveal condition that never arrived is an engine defect."""
    revealed: list[dict[str, Any]] = []

    never = truth_coverage(
        setup(),
        revealed,
        fired_events=["断电"],
        last_game_time=parse_game_time("Day 2 08:00"),
        last_round=5,
    )
    assert set(never["unrevealed"]) == {"q1", "q2"}

    healthy = truth_coverage(
        setup(),
        [{"id": "q1", "text": "谁锁了门？", "answers": []}],
        fired_events=[],
        last_game_time=parse_game_time("Day 1 10:40"),
        last_round=1,
    )
    assert healthy["unrevealed"] == []  # q2's event has not fired yet

    health = build_health(
        rounds=ROUNDS,
        seats=[],
        warnings=[],
        errors=[],
        stop_reason="游戏已结束。",
        coverage={"events": {"overdue": []}, "truths": never},
    )
    issue = next(item for item in health["issues"] if item["code"] == "truth_not_revealed")
    assert "q1" in issue["message"] and "q2" in issue["message"]


def test_health_caps_repeated_narrative_noise() -> None:
    """Many near-identical round pairs collapse into a few issues plus a count."""
    rounds = [
        {"number": index, "narrative": "甲推了推门，门没有开。乙站在旁边看着。"}
        for index in range(1, 8)
    ]

    health = build_health(
        rounds=rounds,
        seats=[],
        warnings=[],
        errors=[],
        stop_reason="游戏已结束。",
        coverage={"events": {}, "truths": {}},
    )

    repeats = [item for item in health["issues"] if item["code"] == "repeated_narrative"]
    assert len(repeats) == 4  # three pairs plus the summary line
    assert "另外还有" in repeats[-1]["message"]


def test_health_reports_card_quality_warnings() -> None:
    """Overdue events, unanswered truths and repeated narrative are surfaced."""
    coverage = {
        "events": {"overdue": ["断电"]},
        "truths": {"enabled": True, "revealed": 1, "answered": 0},
    }
    repeated = [
        {"number": 1, "narrative": "甲推了推门，门没有开。"},
        {"number": 2, "narrative": "甲推了推门，门没有开。"},
    ]

    health = build_health(
        rounds=repeated,
        seats=[
            {
                "character": "甲",
                "failures": 2,
                "retries": 1,
                "fallbacks": 1,
                "forced": 1,
            }
        ],
        warnings=["行动窗口在游戏内时间上原地循环，已改为强制座位提交行动以避免空转。"],
        errors=[],
        stop_reason="游戏已结束。",
        coverage=coverage,
        livelock=True,
    )

    codes = {issue["code"] for issue in health["issues"]}
    assert {"livelock", "event_overdue", "truths_unanswered", "repeated_narrative"} <= codes
    assert health["verdict"] == "ok"  # warnings alone do not fail a finished run
    assert all(issue["level"] != "error" for issue in health["issues"])


def test_health_counts_dm_round_failures() -> None:
    """DM-side failed rounds are reported from the token-usage snapshots."""
    health = build_health(
        rounds=ROUNDS,
        seats=[],
        warnings=[],
        errors=[],
        stop_reason="游戏已结束。",
        coverage={"events": {}, "truths": {}},
        token_usage=[{"round_failures": 2}],
    )

    assert any(issue["code"] == "dm_round_failures" for issue in health["issues"])

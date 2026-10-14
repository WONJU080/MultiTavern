"""Scenario-card loading and offline pre-flight checks."""

import json
from pathlib import Path
from typing import Any

import pytest

from playtest.card import (
    CardFileError,
    CardReport,
    build_payload,
    discover_cards,
    inspect_card,
    read_card,
)


def strict_card(**overrides: Any) -> dict[str, Any]:
    """Return a small but complete strict-mode card payload."""
    payload: dict[str, Any] = {
        "scenario": "【剧本：测试卡】\n\n一间上锁的门。",
        "guidance": "真相：钥匙在花盆下面。",
        "characters": [
            {"name": "甲", "description": "谨慎的锁匠"},
            {"name": "乙", "description": "急躁的记者"},
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
                "arrival_text": "雪从门缝里灌进来。",
                "items": ["key"],
                "links": [{"to": "study"}],
            },
            {"id": "study", "name": "书房", "items": [], "links": []},
        ],
        "items": [{"id": "key", "name": "黄铜钥匙", "region": "hall", "clue": True}],
        "events": [{"name": "敲门声", "day": 1, "minute": 700, "description": "有人在敲门。"}],
        "world": {"start_region": "hall"},
    }
    payload.update(overrides)
    return payload


def write_card(tmp_path: Path, payload: Any, name: str = "card.anyworld.json") -> Path:
    """Write a card file into the test's temporary directory."""
    path = tmp_path / name
    if isinstance(payload, str):
        path.write_text(payload, encoding="utf-8")
    else:
        path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def inspect(tmp_path: Path, payload: Any) -> CardReport:
    """Inspect a payload written as a card file."""
    return inspect_card(write_card(tmp_path, payload))


def codes(report: CardReport) -> set[str]:
    """Return the finding codes of a report."""
    return {finding.code for finding in report.findings}


def level_of(report: CardReport, code: str) -> str:
    """Return the level of one finding code."""
    return next(finding.level for finding in report.findings if finding.code == code)


def test_a_complete_strict_card_passes_cleanly(tmp_path: Path) -> None:
    """A well-formed card validates with no findings at all."""
    report = inspect(tmp_path, strict_card())

    assert report.ok is True
    assert report.schedule == "strict"
    assert report.findings == ()
    assert report.counts() == {
        "characters": 2,
        "regions": 2,
        "items": 1,
        "clue_items": 1,
        "events": 1,
        "time_rules": 1,
        "lorebook": 0,
        "prompt_blocks": 0,
        "truth_questions": 0,
    }
    assert report.title == "测试卡"


def test_a_region_less_card_defaults_to_round_mode(tmp_path: Path) -> None:
    """Without regions the card runs on round scheduling."""
    payload = {
        "scenario": "一场对话。",
        "guidance": "真相：他在撒谎。",
        "characters": [{"name": "甲", "description": "侦探"}],
    }
    report = inspect(tmp_path, payload)

    assert report.ok is True
    assert report.schedule == "round"
    assert report.findings == ()


def test_unknown_top_level_keys_warn(tmp_path: Path) -> None:
    """A misspelled key is ignored by the server, so it must be surfaced here."""
    report = inspect(tmp_path, {**strict_card(), "charcaters": []})

    assert "unknown_key" in codes(report)
    assert level_of(report, "unknown_key") == "warning"


def test_missing_cast_is_reported_with_the_server_wording(tmp_path: Path) -> None:
    """A scenario without a cast cannot start: the server needs characters."""
    payload = {"scenario": "只有一个剧本。", "guidance": "真相。"}
    report = inspect(tmp_path, payload)

    assert report.ok is False
    assert "missing_cast" in codes(report)
    assert "characters" in report.errors[0].message


def test_a_preset_fragment_is_not_a_card(tmp_path: Path) -> None:
    """A prompt-block preset has neither scenario nor cast."""
    payload = {"prompt_blocks": [{"content": "文风：冷硬。", "position": "output"}]}
    report = inspect(tmp_path, payload)

    assert report.ok is False
    assert codes(report) == {"not_a_card"}


def test_cross_check_errors_come_from_the_server_validator(tmp_path: Path) -> None:
    """References that only fail cross-checking still fail the pre-flight."""
    payload = strict_card(
        regions=[
            {"id": "hall", "name": "门厅", "items": ["ghost"], "links": []},
        ],
        items=[{"id": "key", "name": "黄铜钥匙", "region": "hall"}],
        events=[],
    )
    report = inspect(tmp_path, payload)

    assert report.ok is False
    assert codes(report) == {"cross_check"}
    assert "ghost" in report.errors[0].message


def test_unreachable_region_warns(tmp_path: Path) -> None:
    """A region nobody can walk to is dead content."""
    payload = strict_card(
        regions=[
            {"id": "hall", "name": "门厅", "items": [], "links": [{"to": "study"}]},
            {"id": "study", "name": "书房", "items": [], "links": []},
            {"id": "attic", "name": "阁楼", "items": [], "links": []},
        ],
        items=[],
        events=[],
    )
    report = inspect(tmp_path, payload)

    assert report.ok is True
    assert level_of(report, "unreachable_region") == "warning"
    assert "阁楼" in next(
        finding.message for finding in report.findings if finding.code == "unreachable_region"
    )


def test_orphan_clue_item_warns(tmp_path: Path) -> None:
    """An item whose region is not a region can never be discovered."""
    payload = strict_card(
        items=[
            {"id": "key", "name": "黄铜钥匙", "region": "hall"},
            {"id": "photo", "name": "旧照片", "region": "nowhere", "clue": True},
        ],
        events=[],
    )
    report = inspect(tmp_path, payload)

    assert report.ok is True
    assert level_of(report, "orphan_item") == "warning"
    assert "nowhere" in next(
        finding.message for finding in report.findings if finding.code == "orphan_item"
    )


def test_a_gate_on_an_unportable_item_can_never_open(tmp_path: Path) -> None:
    """``requires_item`` on a carryable: false item strands the path."""
    payload = strict_card(
        items=[
            {"id": "key", "name": "黄铜钥匙", "region": "hall", "clue": True},
            {"id": "anchor", "name": "铁锚", "region": "hall", "carryable": False},
        ],
        regions=[
            {
                "id": "hall",
                "name": "门厅",
                "items": ["key", "anchor"],
                "links": [{"to": "study", "requires_item": "anchor"}],
            },
            {"id": "study", "name": "书房", "items": [], "links": []},
        ],
        events=[],
    )
    report = inspect(tmp_path, payload)

    assert report.ok is True
    assert level_of(report, "gated_link_unopenable") == "warning"


@pytest.mark.parametrize(
    "schedule, expected_code, expected_level",
    [("strict", "event_in_past", "warning"), ("round", "event_immediate", "info")],
)
def test_events_scheduled_before_the_opening_are_flagged(
    tmp_path: Path, schedule: str, expected_code: str, expected_level: str
) -> None:
    """A past clock-driven event never fires in strict mode, and fires at once in round."""
    payload = strict_card(
        events=[{"name": "旧事", "day": 1, "minute": 300, "description": "早就发生过。"}],
    )
    if schedule == "round":
        payload.pop("regions")
        payload.pop("items", None)
        payload.pop("world", None)
    report = inspect(tmp_path, payload)

    assert report.ok is True
    assert report.schedule == schedule
    assert level_of(report, expected_code) == expected_level


def test_a_repeating_time_event_is_flagged(tmp_path: Path) -> None:
    """Repeating time events are silently one-shot today, so authors must know."""
    payload = strict_card(
        events=[
            {
                "name": "每小时的钟声",
                "day": 1,
                "minute": 700,
                "description": "钟响了一次。",
                "once": False,
            }
        ],
    )
    report = inspect(tmp_path, payload)

    assert report.ok is True
    assert level_of(report, "repeat_event_unsupported") == "warning"


def test_truth_questions_are_checked_against_the_timeline(tmp_path: Path) -> None:
    """Unreachable reveal conditions are reported for the plugin's questions."""
    payload = strict_card(
        plugins=[
            {
                "type": "possible_truths",
                "enabled": True,
                "config": {
                    "questions": [
                        {
                            "id": "q-event",
                            "text": "谁在敲门？",
                            "condition": "event",
                            "event_name": "没有这个事件",
                        },
                        {
                            "id": "q-past",
                            "text": "几点开饭？",
                            "condition": "time",
                            "day": 1,
                            "minute": 300,
                        },
                        {
                            "id": "q-round",
                            "text": "谁先动手？",
                            "condition": "round",
                            "round_number": 1,
                        },
                    ]
                },
            }
        ],
    )
    report = inspect(tmp_path, payload)

    assert report.ok is True
    assert level_of(report, "truth_event_missing") == "warning"
    assert level_of(report, "truth_time_in_past") == "warning"
    assert level_of(report, "truth_round_immediate") == "info"
    assert report.counts()["truth_questions"] == 3


def test_a_time_condition_without_a_clock_never_reveals(tmp_path: Path) -> None:
    """Time-based reveals need the in-game clock to be enabled."""
    payload = {
        "scenario": "一场对话。",
        "guidance": "真相。",
        "characters": [{"name": "甲", "description": "侦探"}],
        "plugins": [
            {
                "type": "possible_truths",
                "enabled": True,
                "config": {
                    "questions": [
                        {"id": "q", "text": "现在几点？", "condition": "time", "minute": 700}
                    ]
                },
            }
        ],
    }
    report = inspect(tmp_path, payload)

    assert report.ok is True
    assert level_of(report, "truth_time_without_clock") == "warning"


def test_thin_characters_and_missing_guidance_are_noted(tmp_path: Path) -> None:
    """Cards that give a seat nothing to play with are called out."""
    payload = {
        "scenario": "一场对话。",
        "characters": [{"name": "甲"}, {"name": "乙", "personality": "多疑"}],
    }
    report = inspect(tmp_path, payload)

    assert report.ok is True
    assert level_of(report, "thin_character") == "info"
    assert level_of(report, "no_guidance") == "info"
    assert "甲" in next(
        finding.message for finding in report.findings if finding.code == "thin_character"
    )


def test_unreadable_files_are_reported_as_errors(tmp_path: Path) -> None:
    """Broken JSON and a missing file are errors, not exceptions."""
    broken = inspect_card(write_card(tmp_path, "{not json", "broken.anyworld.json"))
    assert broken.ok is False
    assert codes(broken) == {"unreadable"}
    assert broken.digest == ""

    missing = inspect_card(tmp_path / "absent.anyworld.json")
    assert missing.ok is False
    assert "无法读取" in missing.errors[0].message

    non_object = inspect_card(write_card(tmp_path, [1, 2, 3], "list.anyworld.json"))
    assert non_object.ok is False
    assert "顶层必须是 JSON 对象" in non_object.errors[0].message


def test_digest_is_content_addressed(tmp_path: Path) -> None:
    """The digest identifies the exact bytes an author validated."""
    path = write_card(tmp_path, strict_card())
    first_raw, first_digest = read_card(path)
    _, second_digest = read_card(path)

    assert first_digest == second_digest
    assert len(first_digest) == 64
    assert set(build_payload(first_raw)) <= set(first_raw)


def test_discover_cards_lists_only_cards(tmp_path: Path) -> None:
    """Directory scanning picks up cards and ignores everything else."""
    write_card(tmp_path, strict_card(), "a.anyworld.json")
    write_card(tmp_path, strict_card(), "b.anyworld.json")
    (tmp_path / "notes.md").write_text("not a card", encoding="utf-8")
    (tmp_path / "nested").mkdir()

    found = [path.name for path in discover_cards(tmp_path)]

    assert found == ["a.anyworld.json", "b.anyworld.json"]
    with pytest.raises(CardFileError):
        discover_cards(tmp_path / "absent")


@pytest.mark.parametrize(
    "kind, expected_ok, expected_schedule, expected_code",
    [
        ("strict", True, "strict", None),
        ("round", True, "round", None),
        ("no-cast", False, None, "missing_cast"),
        ("preset", False, None, "not_a_card"),
    ],
)
def test_card_shapes_are_classified(
    tmp_path: Path,
    kind: str,
    expected_ok: bool,
    expected_schedule: str | None,
    expected_code: str | None,
) -> None:
    """Each card shape an author can produce is judged as they would expect.

    The fixtures are built here on purpose: the repository ships the *format*,
    not any particular scenario, so nothing in the suite depends on (or reveals)
    somebody's own cards.
    """
    world_keys = {"time_config", "time_rules", "regions", "items", "events", "world"}
    if kind == "preset":
        payload: dict = {"name": "预设", "temperature": 0.8, "prompts": []}
    elif kind == "no-cast":
        payload = {key: value for key, value in strict_card().items() if key != "characters"}
    elif kind == "round":
        # A round-mode card is the same payload without the world/time system.
        payload = {key: value for key, value in strict_card().items() if key not in world_keys}
    else:
        payload = strict_card()
    path = tmp_path / f"{kind}.anyworld.json"
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")

    report = inspect_card(path)

    assert report.ok is expected_ok, report.findings
    assert report.schedule == expected_schedule
    if expected_code is not None:
        assert expected_code in codes(report)

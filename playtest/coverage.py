"""Card coverage and run health: what the playtest actually exercised.

Everything here is computed from the card plus the run's public record, so it
needs no server and stays honest: coverage is measured from what players really
saw (the shared map, the codex, revealed questions), never from what the DM
knows privately.

Two caveats are deliberate and labelled in the output:

* lorebook activation is not disclosed to clients, so it is approximated by
  keyword hits in the public text;
* a region or item that no seat ever reached is reported as unvisited rather
  than missing — that is exactly the signal a card author wants.
"""

from __future__ import annotations

from typing import Any, Mapping, Sequence

from logic.lobby import ScenarioSetup
from logic.plugins import truths_config

MINUTES_PER_DAY = 1440
REPEATED_NARRATIVE_THRESHOLD = 0.6
MAX_REPORTED_REPEATS = 3
LOREBOOK_TEXT_LIMIT = 200_000


def parse_game_time(text: str) -> int | None:
    """Parse a ``Day d hh:mm`` label into absolute in-game minutes."""
    if not isinstance(text, str):
        return None
    parts = text.strip().split()
    if len(parts) != 3 or parts[0].casefold() != "day":
        return None
    try:
        day = int(parts[1])
        hours, minutes = parts[2].split(":")
        return day * MINUTES_PER_DAY + int(hours) * 60 + int(minutes)
    except (ValueError, IndexError):
        return None


def public_text(rounds: Sequence[Mapping[str, Any]]) -> str:
    """Join everything a player could have read during the run."""
    chunks: list[str] = []
    for record in rounds:
        chunks.append(str(record.get("narrative") or ""))
        for action in (record.get("actions") or {}).values():
            chunks.append(str(action))
        for outcome in (record.get("outcomes") or {}).values():
            chunks.append(str(outcome))
    return "\n".join(chunks)[:LOREBOOK_TEXT_LIMIT]


def region_coverage(setup: ScenarioSetup, map_state: Mapping[str, Any] | None) -> dict[str, Any]:
    """Compare visited regions against the card's region graph."""
    total = [region.id for region in setup.regions]
    if not total:
        return {"total": 0, "visited": 0, "visited_names": [], "unvisited_names": []}
    state = map_state or {}
    visited_ids = {
        str(region.get("id"))
        for region in state.get("regions") or []
        if isinstance(region, Mapping)
    }
    names = {region.id: region.name for region in setup.regions}
    visited_names = [names[item] for item in total if item in visited_ids]
    unvisited_names = [names[item] for item in total if item not in visited_ids]
    return {
        "total": len(total),
        "visited": len(visited_names),
        "visited_names": visited_names,
        "unvisited_names": unvisited_names,
    }


def item_coverage(
    setup: ScenarioSetup,
    map_state: Mapping[str, Any] | None,
    codex_state: Mapping[str, Any] | None,
) -> dict[str, Any]:
    """Compare discovered, investigated and carried items against the card."""
    visible = [item for item in setup.items if not item.backstage]
    codex_items = [
        item for item in (codex_state or {}).get("items") or [] if isinstance(item, Mapping)
    ]
    discovered_ids = {str(item.get("id")) for item in codex_items}
    investigated_ids = {str(item.get("id")) for item in codex_items if item.get("investigated")}
    carried: set[str] = set()
    for player in (map_state or {}).get("players") or []:
        if not isinstance(player, Mapping):
            continue
        for name in player.get("carried") or []:
            carried.add(str(name))
    names = {item.id: item.name for item in setup.items}
    clues = [item for item in visible if item.clue or item.key_item]
    return {
        "total": len(visible),
        "backstage_total": len(setup.items) - len(visible),
        "discovered": len(discovered_ids & set(names)),
        "investigated": len(investigated_ids & set(names)),
        "discovered_names": [names[item] for item in names if item in discovered_ids],
        "investigated_names": [names[item] for item in names if item in investigated_ids],
        "carried_names": sorted(carried),
        "clue_total": len(clues),
        "clue_discovered": len([item for item in clues if item.id in discovered_ids]),
        "clue_investigated": len([item for item in clues if item.id in investigated_ids]),
    }


def _clock_driven(event: Any) -> bool:
    """Return whether an event fires from the clock rather than on entry."""
    return (not event.region) or event.when == "time"


def event_coverage(
    setup: ScenarioSetup,
    codex_state: Mapping[str, Any] | None,
    last_game_time: int | None,
) -> dict[str, Any]:
    """Compare fired events against the card, including overdue ones."""
    fired = {
        str(entry.get("name"))
        for entry in (codex_state or {}).get("events") or []
        if isinstance(entry, Mapping)
    }
    names = [event.name for event in setup.events]
    overdue: list[str] = []
    for event in setup.events:
        if event.name in fired or not _clock_driven(event):
            continue
        if last_game_time is None:
            continue
        if event.day * MINUTES_PER_DAY + event.minute <= last_game_time:
            overdue.append(event.name)
    return {
        "total": len(names),
        "fired": len([name for name in names if name in fired]),
        "fired_names": [name for name in names if name in fired],
        "overdue": overdue,
    }


def lorebook_coverage(setup: ScenarioSetup, text: str) -> dict[str, Any]:
    """Approximate world-book activation by keyword hits in the public text."""
    enabled = [entry for entry in setup.lorebook if entry.enabled]
    if not enabled:
        return {"total": 0, "hit": 0, "hit_titles": [], "method": "keyword"}
    haystack = text.casefold()
    hits = [
        (entry.title or entry.keys[0])
        for entry in enabled
        if any(key.casefold() in haystack for key in entry.keys)
    ]
    return {
        "total": len(enabled),
        "hit": len(hits),
        "hit_titles": hits,
        "method": "keyword",
    }


def truth_coverage(
    setup: ScenarioSetup,
    revealed: Sequence[Mapping[str, Any]],
    *,
    fired_events: Sequence[str] = (),
    last_game_time: int | None = None,
    last_round: int = 0,
) -> dict[str, Any]:
    """Compare revealed and answered questions with the card.

    Questions whose reveal condition the run itself satisfied but that never
    arrived are listed as ``unrevealed``: that is a server-side defect rather
    than a card problem, and the report says so instead of hiding it.
    """
    config = truths_config(setup.plugins)
    questions = list(config.questions) if config is not None else []
    revealed_ids = {str(item.get("id")) for item in revealed}
    answered = [
        str(item.get("id"))
        for item in revealed
        if [entry for entry in item.get("answers") or [] if isinstance(entry, Mapping)]
    ]
    events = {str(name) for name in fired_events}
    unrevealed: list[str] = []
    for question in questions:
        if question.id in revealed_ids:
            continue
        if question.condition == "start":
            unrevealed.append(question.id)
        elif question.condition == "event" and question.event_name in events:
            unrevealed.append(question.id)
        elif (
            question.condition == "time"
            and last_game_time is not None
            and last_game_time >= question.day * MINUTES_PER_DAY + question.minute
        ):
            unrevealed.append(question.id)
        elif question.condition == "round" and last_round >= question.round_number:
            unrevealed.append(question.id)
    return {
        "enabled": config is not None,
        "total": len(questions),
        "revealed": len([item for item in questions if item.id in revealed_ids]),
        "answered": len(answered),
        "revealed_ids": sorted(revealed_ids),
        "answered_ids": answered,
        "unrevealed": unrevealed,
        "claims": [
            {
                "question_id": str(item.get("id")),
                "character": str(entry.get("character")),
                "text": str(entry.get("text")),
            }
            for item in revealed
            for entry in item.get("answers") or []
            if isinstance(entry, Mapping)
        ],
    }


def time_coverage(rounds: Sequence[Mapping[str, Any]]) -> dict[str, Any]:
    """Report how far the in-game clock moved across the run."""
    stamps = [
        (record.get("number"), parse_game_time(str(record.get("game_time") or "")))
        for record in rounds
    ]
    known = [(number, value) for number, value in stamps if value is not None]
    if not known:
        return {"start": "", "end": "", "elapsed_minutes": 0, "boundaries": len(rounds)}
    start_number, start = known[0]
    end_number, end = known[-1]
    labels = {record.get("number"): str(record.get("game_time") or "") for record in rounds}
    return {
        "start": labels.get(start_number, ""),
        "end": labels.get(end_number, ""),
        "elapsed_minutes": max(0, end - start),
        "boundaries": len(rounds),
    }


def participation(
    setup: ScenarioSetup, rounds: Sequence[Mapping[str, Any]]
) -> list[dict[str, Any]]:
    """Report per-character involvement across the completed rounds."""
    rows: list[dict[str, Any]] = []
    for character in setup.cast:
        name = character.name
        action_rounds = 0
        mentioned_rounds = 0
        longest_gap = 0
        gap = 0
        for record in rounds:
            if not record.get("narrative"):
                continue
            actions = record.get("actions") or {}
            if name in actions:
                action_rounds += 1
                gap = 0
            else:
                gap += 1
                longest_gap = max(longest_gap, gap)
            text = str(record.get("narrative") or "")
            others = " ".join(
                str(value) for key, value in (record.get("outcomes") or {}).items() if key != name
            )
            if name in text or name in others:
                mentioned_rounds += 1
        rows.append(
            {
                "character": name,
                "action_rounds": action_rounds,
                "mentioned_rounds": mentioned_rounds,
                "longest_gap": longest_gap,
            }
        )
    return rows


def narrative_similarity(rounds: Sequence[Mapping[str, Any]]) -> list[dict[str, Any]]:
    """Compare consecutive round narratives with a 3-gram Jaccard score."""
    completed = [record for record in rounds if record.get("narrative")]
    pairs: list[dict[str, Any]] = []
    for previous, current in zip(completed, completed[1:]):
        score = _jaccard(
            _shingles(str(previous.get("narrative") or "")),
            _shingles(str(current.get("narrative") or "")),
        )
        pairs.append(
            {
                "from": previous.get("number"),
                "to": current.get("number"),
                "similarity": round(score, 3),
            }
        )
    return pairs


def _shingles(text: str, size: int = 3) -> set[str]:
    """Return character n-grams for a similarity comparison."""
    collapsed = "".join(text.split())
    if len(collapsed) <= size:
        return {collapsed} if collapsed else set()
    return {collapsed[index : index + size] for index in range(len(collapsed) - size + 1)}


def _jaccard(left: set[str], right: set[str]) -> float:
    """Return the Jaccard similarity of two token sets."""
    if not left or not right:
        return 0.0
    return len(left & right) / len(left | right)


def build_coverage(
    *,
    setup: ScenarioSetup,
    rounds: Sequence[Mapping[str, Any]],
    map_state: Mapping[str, Any] | None,
    codex_state: Mapping[str, Any] | None,
    revealed_truths: Sequence[Mapping[str, Any]],
) -> dict[str, Any]:
    """Assemble the whole coverage section of a run report."""
    times = time_coverage(rounds)
    last_game_time = parse_game_time(str(times.get("end") or ""))
    text = public_text(rounds)
    events = event_coverage(setup, codex_state, last_game_time)
    completed = [record.get("number") for record in rounds if record.get("narrative")]
    return {
        "regions": region_coverage(setup, map_state),
        "items": item_coverage(setup, map_state, codex_state),
        "events": events,
        "lorebook": lorebook_coverage(setup, text),
        "truths": truth_coverage(
            setup,
            revealed_truths,
            fired_events=events["fired_names"],
            last_game_time=last_game_time,
            last_round=max([number for number in completed if isinstance(number, int)], default=0),
        ),
        "time": times,
        "participation": participation(setup, rounds),
    }


def build_health(
    *,
    rounds: Sequence[Mapping[str, Any]],
    seats: Sequence[Mapping[str, Any]],
    warnings: Sequence[str],
    errors: Sequence[str],
    stop_reason: str,
    rejections: Sequence[str] = (),
    pauses: Sequence[str] = (),
    coverage: Mapping[str, Any],
    stalled: bool = False,
    livelock: bool = False,
    host_lost: bool = False,
    token_usage: Sequence[Mapping[str, Any]] = (),
) -> dict[str, Any]:
    """Grade a run: fatal problems, card-quality warnings and informational notes."""
    issues: list[dict[str, str]] = []
    if host_lost:
        issues.append({"level": "error", "code": "host_lost", "message": "主机连接中断。"})
    if stalled:
        issues.append({"level": "error", "code": "stalled", "message": "长时间没有任何事件。"})
    if errors:
        issues.append(
            {
                "level": "error",
                "code": "server_errors",
                "message": "服务器返回了错误：" + "；".join(errors[:3]),
            }
        )
    if livelock:
        issues.append(
            {
                "level": "warning",
                "code": "livelock",
                "message": "行动窗口一度在游戏内时间上原地循环，已由强制行动解除。",
            }
        )
    repeats = [
        row
        for row in narrative_similarity(rounds)
        if row["similarity"] >= REPEATED_NARRATIVE_THRESHOLD
    ]
    for row in repeats[:MAX_REPORTED_REPEATS]:
        issues.append(
            {
                "level": "warning",
                "code": "repeated_narrative",
                "message": (
                    f"第{row['from']}与第{row['to']}回合的叙述高度相似"
                    f"（{row['similarity']:.2f}），可能出现原地打转。"
                ),
            }
        )
    if len(repeats) > MAX_REPORTED_REPEATS:
        issues.append(
            {
                "level": "warning",
                "code": "repeated_narrative",
                "message": f"另外还有 {len(repeats) - MAX_REPORTED_REPEATS} 对相邻回合叙述高度相似。",
            }
        )
    if pauses:
        issues.append(
            {
                "level": "info",
                "code": "dm_round_paused",
                "message": (
                    f"DM 结算失败 {len(pauses)} 次，已自动请房主重试："
                    + "；".join(list(pauses)[:2])
                ),
            }
        )
    if rejections:
        issues.append(
            {
                "level": "info",
                "code": "action_rejected",
                "message": (
                    f"有 {len(rejections)} 类提交被裁判驳回，驱动已自动重试或改用兜底行动："
                    + "；".join(list(rejections)[:3])
                ),
            }
        )
    for seat in seats:
        if seat.get("failures"):
            issues.append(
                {
                    "level": "info",
                    "code": "seat_failures",
                    "message": f"{seat.get('character')} 座位调用失败 {seat['failures']} 次。",
                }
            )
        if seat.get("forced"):
            issues.append(
                {
                    "level": "info",
                    "code": "seat_forced",
                    "message": f"{seat.get('character')} 有 {seat['forced']} 次强制行动。",
                }
            )
    overdue = list((coverage.get("events") or {}).get("overdue") or [])
    if overdue:
        issues.append(
            {
                "level": "warning",
                "code": "event_overdue",
                "message": "已过触发时刻但仍未发生的事件：" + "、".join(overdue),
            }
        )
    truth = coverage.get("truths") or {}
    if truth.get("enabled") and truth.get("revealed") and not truth.get("answered"):
        issues.append(
            {
                "level": "warning",
                "code": "truths_unanswered",
                "message": "有真相提问已揭晓，但没有任何角色给出可判定的陈述。",
            }
        )
    unrevealed = list(truth.get("unrevealed") or [])
    if unrevealed:
        issues.append(
            {
                "level": "warning",
                "code": "truth_not_revealed",
                "message": (
                    "这些真相提问的揭晓条件已经满足，但服务器始终没有揭晓："
                    + "、".join(unrevealed)
                    + "（疑似引擎缺陷，不是卡的问题）"
                ),
            }
        )
    failures = sum(
        int(record.get("round_failures") or 0)
        for record in token_usage
        if isinstance(record, Mapping)
    )
    if failures:
        issues.append(
            {
                "level": "warning",
                "code": "dm_round_failures",
                "message": f"DM 结算失败 {failures} 次（可能触发过暂停与重试）。",
            }
        )
    unfinished = any(marker in stop_reason for marker in ("回合上限", "时间预算", "token 预算"))
    failed = any(issue["level"] == "error" for issue in issues)
    verdict = "failed" if failed else ("unfinished" if unfinished else "ok")
    return {
        "verdict": verdict,
        "issues": issues,
        "narrative_similarity": narrative_similarity(rounds),
        "warnings": list(warnings),
    }


def summarize_coverage(coverage: Mapping[str, Any]) -> str:
    """Render one human-readable coverage line."""
    regions = coverage.get("regions") or {}
    items = coverage.get("items") or {}
    events = coverage.get("events") or {}
    truths = coverage.get("truths") or {}
    time_info = coverage.get("time") or {}
    return (
        f"地区 {regions.get('visited', 0)}/{regions.get('total', 0)} · "
        f"物品 {items.get('discovered', 0)}/{items.get('total', 0)}"
        f"（已调查 {items.get('investigated', 0)}）· "
        f"事件 {events.get('fired', 0)}/{events.get('total', 0)} · "
        f"真相 {truths.get('revealed', 0)}/{truths.get('total', 0)}"
        f"（作答 {truths.get('answered', 0)}）· "
        f"时间推进 {time_info.get('elapsed_minutes', 0)} 分钟"
    )

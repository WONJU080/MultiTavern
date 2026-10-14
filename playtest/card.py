"""Scenario-card loading and offline pre-flight inspection.

A card is a JSON file whose keys map directly onto the server's
``scenario_init`` payload. Loading is deliberately cheap: inspection must be
able to tell an author that a card would be rejected, or would strand its own
clues, without spending a single token on inference.
"""

from __future__ import annotations

import hashlib
import json
from collections import deque
from collections.abc import Callable, Iterable, Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal

from core.config import settings
from logic.lobby import (
    ScenarioSetup,
    parse_cast,
    parse_events,
    parse_items,
    parse_lorebook,
    parse_prompt_blocks,
    parse_regions,
    parse_sampling,
    parse_scenario_setup,
    parse_time_config,
    parse_time_rules,
)
from logic.plugins import parse_plugins, truths_config

CARD_SUFFIX = ".anyworld.json"
MINUTES_PER_DAY = 1440

# Every key the server reads from scenario_init; anything else is a typo.
SCENARIO_KEYS: tuple[str, ...] = (
    "scenario",
    "opening",
    "guidance",
    "characters",
    "host_character",
    "time_config",
    "time_rules",
    "events",
    "world",
    "regions",
    "items",
    "lorebook",
    "prompt_blocks",
    "sampling",
    "plugins",
    "random_turn_order",
    "schedule",
)

# Field parsers reused from the server so a card cannot pass here and fail there.
FIELD_PARSERS: tuple[tuple[str, Callable[[object], object]], ...] = (
    ("characters", parse_cast),
    ("time_config", parse_time_config),
    ("time_rules", parse_time_rules),
    ("events", parse_events),
    ("regions", parse_regions),
    ("items", parse_items),
    ("lorebook", parse_lorebook),
    ("prompt_blocks", parse_prompt_blocks),
    ("sampling", parse_sampling),
    ("plugins", parse_plugins),
)

FindingLevel = Literal["error", "warning", "info"]


class CardFileError(ValueError):
    """Raised when a card file cannot be read as a JSON object at all."""


@dataclass(frozen=True, slots=True)
class Finding:
    """One pre-flight result: an error, a warning, or an informational note."""

    level: FindingLevel
    code: str
    message: str

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-serializable representation."""
        return {"level": self.level, "code": self.code, "message": self.message}


@dataclass(frozen=True, slots=True)
class CardReport:
    """A loaded card plus everything the offline pre-flight can say about it."""

    path: Path
    digest: str
    payload: dict[str, Any]
    setup: ScenarioSetup | None
    findings: tuple[Finding, ...]

    @property
    def errors(self) -> list[Finding]:
        """Return blocking problems."""
        return [item for item in self.findings if item.level == "error"]

    @property
    def warnings(self) -> list[Finding]:
        """Return non-blocking problems worth fixing."""
        return [item for item in self.findings if item.level == "warning"]

    @property
    def infos(self) -> list[Finding]:
        """Return informational notes."""
        return [item for item in self.findings if item.level == "info"]

    @property
    def ok(self) -> bool:
        """Return whether the server would accept this card."""
        return not self.errors

    @property
    def schedule(self) -> str | None:
        """Return the resolved scheduling mode, when the card validated."""
        return self.setup.schedule if self.setup is not None else None

    @property
    def title(self) -> str:
        """Return a display name derived from the scenario's first heading."""
        text = str(self.payload.get("scenario", "")).strip()
        first = next((line.strip() for line in text.splitlines() if line.strip()), "")
        cleaned = _strip_wrapping(first)
        head, separator, tail = cleaned.partition("：")
        if separator and len(head) <= 8 and tail.strip():
            cleaned = _strip_wrapping(tail.strip())
        return cleaned or self.path.name

    def counts(self) -> dict[str, int]:
        """Return the structural size of the card."""
        setup = self.setup
        if setup is None:
            return {}
        return {
            "characters": len(setup.cast),
            "regions": len(setup.regions),
            "items": len(setup.items),
            "clue_items": sum(1 for item in setup.items if item.clue),
            "events": len(setup.events),
            "time_rules": len(setup.time_rules),
            "lorebook": len(setup.lorebook),
            "prompt_blocks": len(setup.prompt_blocks),
            "truth_questions": len(_truth_questions(setup)),
        }

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "path": str(self.path),
            "digest": self.digest,
            "title": self.title,
            "ok": self.ok,
            "schedule": self.schedule,
            "counts": self.counts(),
            "findings": [item.as_dict() for item in self.findings],
        }


_WRAPPING_PAIRS = {"《": "》", "【": "】", "[": "]", "（": "）", "(": ")", "「": "」", "“": "”"}


def _strip_wrapping(text: str) -> str:
    """Remove matched wrapping brackets from a card heading."""
    cleaned = text.strip()
    while len(cleaned) >= 2 and _WRAPPING_PAIRS.get(cleaned[0]) == cleaned[-1]:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def read_card(path: str | Path) -> tuple[dict[str, Any], str]:
    """Read one card file as a JSON object and return it with its digest."""
    card_path = Path(path)
    try:
        text = card_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise CardFileError(f"无法读取 {card_path}: {exc}") from exc
    try:
        raw = json.loads(text)
    except json.JSONDecodeError as exc:
        raise CardFileError(f"{card_path} 不是合法 JSON: {exc}") from exc
    if not isinstance(raw, dict):
        raise CardFileError(f"{card_path} 的顶层必须是 JSON 对象。")
    return raw, hashlib.sha256(text.encode("utf-8")).hexdigest()


def build_payload(raw: Mapping[str, Any]) -> dict[str, Any]:
    """Keep only the keys the server reads, in a stable order."""
    return {key: raw[key] for key in SCENARIO_KEYS if key in raw}


def discover_cards(directory: str | Path) -> list[Path]:
    """Return every card file in a directory, sorted by name."""
    base = Path(directory)
    if not base.is_dir():
        raise CardFileError(f"{base} 不是目录。")
    return sorted(path for path in base.glob(f"*{CARD_SUFFIX}") if path.is_file())


def _completeness_findings(payload: dict[str, Any]) -> list[Finding]:
    """Report the difference between a playable card and a partial artifact."""
    has_scenario = "scenario" in payload
    has_cast = "characters" in payload
    if not has_scenario and not has_cast:
        return [
            Finding(
                "error",
                "not_a_card",
                "既没有 'scenario' 也没有 'characters'：这看起来是预设或片段，不是可玩的剧情卡。",
            )
        ]
    if not has_cast:
        return [
            Finding(
                "error",
                "missing_cast",
                "缺少 'characters' 角色名单：服务器会以“'characters' 必须是非空列表”拒收；"
                "这张卡目前只有剧本正文。",
            )
        ]
    if not has_scenario:
        return [
            Finding(
                "error",
                "missing_scenario",
                "缺少 'scenario' 剧本正文：服务器会以“'scenario' 必须是字符串”拒收。",
            )
        ]
    return []


def inspect_card(path: str | Path) -> CardReport:
    """Validate one card and report every problem the offline checks can find."""
    card_path = Path(path)
    try:
        raw, digest = read_card(card_path)
    except CardFileError as exc:
        return CardReport(
            path=card_path,
            digest="",
            payload={},
            setup=None,
            findings=(Finding("error", "unreadable", str(exc)),),
        )

    findings: list[Finding] = []
    for key in raw:
        if key not in SCENARIO_KEYS:
            findings.append(
                Finding(
                    "warning",
                    "unknown_key",
                    f"未知字段 '{key}'：服务器会忽略它，可能是拼写错误。",
                )
            )
    payload = build_payload(raw)
    findings.extend(_completeness_findings(payload))
    # Collect every field error at once instead of only the first one.
    for key, parser in FIELD_PARSERS:
        if key not in payload:
            continue
        try:
            parser(payload[key])
        except ValueError as exc:
            findings.append(Finding("error", f"field:{key}", f"{key}: {exc}"))

    setup: ScenarioSetup | None = None
    if not any(item.level == "error" for item in findings):
        try:
            setup = parse_scenario_setup(payload)
        except ValueError as exc:
            findings.append(Finding("error", "cross_check", str(exc)))
    if setup is not None:
        findings.extend(structural_findings(setup))
    return CardReport(
        path=card_path,
        digest=digest,
        payload=payload,
        setup=setup,
        findings=tuple(findings),
    )


def _truth_questions(setup: ScenarioSetup) -> list[Any]:
    """Return the configured possible-truth questions, if the plugin is on."""
    config = truths_config(setup.plugins)
    return list(config.questions) if config is not None else []


def _clock_minutes(day: int, minute: int) -> int:
    """Return an absolute in-game minute count."""
    return day * MINUTES_PER_DAY + minute


def _reachable_regions(start: str, regions: Iterable[Any]) -> set[str]:
    """Return every region reachable from ``start`` ignoring item gates."""
    graph = {region.id: [link.to for link in region.links] for region in regions}
    seen = {start}
    queue = deque([start])
    while queue:
        current = queue.popleft()
        for neighbour in graph.get(current, []):
            if neighbour not in seen:
                seen.add(neighbour)
                queue.append(neighbour)
    return seen


def structural_findings(setup: ScenarioSetup) -> list[Finding]:
    """Return static design problems in an otherwise valid card."""
    findings: list[Finding] = []
    findings.extend(_region_findings(setup))
    findings.extend(_time_findings(setup))
    findings.extend(_truth_findings(setup))
    findings.extend(_content_findings(setup))
    return findings


def _region_findings(setup: ScenarioSetup) -> list[Finding]:
    """Check the region graph, item placement and item-gated links."""
    findings: list[Finding] = []
    regions = setup.regions
    if not regions:
        if setup.items:
            findings.append(
                Finding(
                    "info",
                    "items_without_regions",
                    "卡里有物品但没有地区：物品只能靠 DM 叙事提及，无法被调查或拾取。",
                )
            )
        return findings

    start = setup.start_region or regions[0].id
    reachable = _reachable_regions(start, regions)
    region_ids = {region.id for region in regions}
    names = {region.id: region.name for region in regions}
    placed = {item_id for region in regions for item_id in region.items}
    items = {item.id: item for item in setup.items}

    for region in regions:
        if region.id not in reachable:
            findings.append(
                Finding(
                    "warning",
                    "unreachable_region",
                    f"地区「{region.name}」（{region.id}）从 start_region 出发不可达"
                    "（忽略 requires_item 门禁后仍不可达）。",
                )
            )
    for item in setup.items:
        if item.region and item.region not in region_ids:
            level: FindingLevel = "warning" if (item.clue or item.key_item) else "info"
            findings.append(
                Finding(
                    level,
                    "orphan_item",
                    f"物品「{item.name}」（{item.id}）的 region='{item.region}' 不是任何已定义地区，"
                    "它不会出现在地图上（服务器不校验这一条）。",
                )
            )
        elif not item.region and item.id not in placed:
            findings.append(
                Finding(
                    "info",
                    "floating_item",
                    f"物品「{item.name}」（{item.id}）没有归属地区也没有被任何地区列出，"
                    "只能靠 DM 叙事浮现。",
                )
            )
        if (item.clue or item.key_item) and item.region in region_ids:
            if item.region not in reachable:
                findings.append(
                    Finding(
                        "warning",
                        "unreachable_clue",
                        f"关键物品「{item.name}」位于不可达地区「{names.get(item.region, item.region)}」，"
                        "相关线索无法被玩家发现。",
                    )
                )
    for region in regions:
        for link in region.links:
            if not link.requires_item:
                continue
            item = items.get(link.requires_item)
            if item is None or item.carryable:
                continue
            findings.append(
                Finding(
                    "warning",
                    "gated_link_unopenable",
                    f"连接「{region.id} → {link.to}」需要物品「{item.name}」，"
                    "但该物品 carryable: false（永远无法被携带），这条连接打不开。",
                )
            )
    return findings


def _time_findings(setup: ScenarioSetup) -> list[Finding]:
    """Check clock configuration and clock-driven event scheduling."""
    findings: list[Finding] = []
    config = setup.time_config
    if setup.time_rules and not config.enabled:
        findings.append(
            Finding(
                "info",
                "rules_unused",
                "配置了 time_rules 但未启用游戏内时钟：这些耗时规则不会被使用。",
            )
        )
    if not config.enabled:
        return findings

    start_total = _clock_minutes(config.start_day, config.start_minute)
    for event in setup.events:
        clock_driven = not event.region or event.when == "time"
        if not clock_driven:
            continue
        due = _clock_minutes(event.day, event.minute)
        if due <= start_total:
            if setup.schedule == "strict":
                findings.append(
                    Finding(
                        "warning",
                        "event_in_past",
                        f"定时事件「{event.name}」的时刻不晚于开局时间，"
                        "时间轴模式只查找晚于当前时刻的事件，它永远不会触发。",
                    )
                )
            else:
                findings.append(
                    Finding(
                        "info",
                        "event_immediate",
                        f"定时事件「{event.name}」的时刻不晚于开局时间，"
                        "它会在第一回合结算时立刻触发。",
                    )
                )
        if not event.once:
            findings.append(
                Finding(
                    "warning",
                    "repeat_event_unsupported",
                    f"事件「{event.name}」设置了 once: false，但时间事件目前只会触发一次"
                    "（只有 when: 'enter' 的地区事件会尊重 once）。",
                )
            )
    if setup.schedule == "strict" and not setup.events:
        findings.append(
            Finding(
                "info",
                "strict_without_events",
                "时间轴模式没有任何定时事件：游戏内时间只会随行动耗时推进。",
            )
        )
    return findings


def _truth_findings(setup: ScenarioSetup) -> list[Finding]:
    """Check possible-truth reveal conditions against the card's own timeline."""
    findings: list[Finding] = []
    questions = _truth_questions(setup)
    if not questions:
        return findings
    event_names = {event.name for event in setup.events}
    start_total = _clock_minutes(setup.time_config.start_day, setup.time_config.start_minute)
    for question in questions:
        label = question.id or question.text[:12]
        if question.condition == "event" and question.event_name not in event_names:
            findings.append(
                Finding(
                    "warning",
                    "truth_event_missing",
                    f"提问「{label}」依赖事件「{question.event_name}」，卡里没有同名事件，"
                    "它永远不会揭晓。",
                )
            )
        elif question.condition == "time":
            if not setup.time_config.enabled:
                findings.append(
                    Finding(
                        "warning",
                        "truth_time_without_clock",
                        f"提问「{label}」以游戏内时间揭晓，但时钟未启用，它永远不会揭晓。",
                    )
                )
            elif _clock_minutes(question.day, question.minute) <= start_total:
                findings.append(
                    Finding(
                        "warning",
                        "truth_time_in_past",
                        f"提问「{label}」的揭晓时刻不晚于开局时间，时间轴模式下永远不会揭晓。",
                    )
                )
        elif question.condition == "round" and question.round_number <= 1:
            findings.append(
                Finding(
                    "info",
                    "truth_round_immediate",
                    f"提问「{label}」在第 {question.round_number} 轮揭晓，开局后很快就会出现。",
                )
            )
    return findings


def _content_findings(setup: ScenarioSetup) -> list[Finding]:
    """Check cast and prompt material that shapes how the card actually plays."""
    findings: list[Finding] = []
    limit = settings.server.max_characters
    if len(setup.cast) > limit:
        findings.append(
            Finding(
                "warning",
                "cast_over_local_limit",
                f"角色数 {len(setup.cast)} 超过本机 config.yaml 的 max_characters={limit}；"
                "目标服务器若用同一配置会拒绝这张卡。",
            )
        )
    thin = [
        character.name
        for character in setup.cast
        if not character.description.strip() and not character.personality.strip()
    ]
    if thin:
        findings.append(
            Finding(
                "info",
                "thin_character",
                "以下角色既没有 description 也没有 personality，扮演它的座位只能靠名字猜行为："
                + "、".join(thin),
            )
        )
    if not setup.guidance.strip():
        findings.append(
            Finding(
                "info",
                "no_guidance",
                "没有私密手册（guidance）：DM 只能依赖 scenario 正文，伏笔与真相更容易跑偏。",
            )
        )
    return findings

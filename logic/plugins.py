"""Per-room plugin configuration and possible-truths runtime helpers."""

from __future__ import annotations

from core.schemas import PossibleTruthsConfig, RoomPlugin, TruthQuestion
from logic.validation import clean_optional_text, clean_text

MAX_PLUGINS = 20
MAX_QUESTIONS = 100


def _bounded(value: object, field: str, default: int, maximum: int) -> int:
    """Return an integer within [0, maximum], or the default for None."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{field}' 必须是整数")
    if value < 0 or value > maximum:
        raise ValueError(f"'{field}' 必须在 0 到 {maximum} 之间")
    return value


def _parse_bool(value: object, field: str, default: bool) -> bool:
    if value is None:
        return default
    if not isinstance(value, bool):
        raise ValueError(f"'{field}' 必须是布尔值")
    return value


def _parse_truths_config(raw: object) -> PossibleTruthsConfig:
    """Validate the possible-truths plugin config and normalize its questions."""
    if not isinstance(raw, dict):
        raise ValueError("'possible_truths.config' 必须是对象")
    questions_raw = raw.get("questions", [])
    if not isinstance(questions_raw, list):
        raise ValueError("'possible_truths.questions' 必须是列表")
    if len(questions_raw) > MAX_QUESTIONS:
        raise ValueError(f"最多允许 {MAX_QUESTIONS} 个提问")
    questions: list[TruthQuestion] = []
    seen: set[str] = set()
    for entry in questions_raw:
        if not isinstance(entry, dict):
            raise ValueError("每个提问必须是含 'id' 和 'text' 的对象")
        question_id = clean_text(entry.get("id"), "question id", 40)
        if question_id.casefold() in seen:
            raise ValueError("提问 id 不能重复。")
        seen.add(question_id.casefold())
        condition = entry.get("condition", "start")
        if condition not in ("start", "event", "time", "round"):
            raise ValueError(f"未知的提问条件 '{condition}'。")
        event_name = clean_optional_text(entry.get("event_name"), "event_name", 80)
        if condition == "event" and not event_name:
            raise ValueError("event 条件必须提供 event_name。")
        questions.append(
            TruthQuestion(
                id=question_id,
                text=clean_text(entry.get("text"), "question text", 2_000),
                condition=condition,
                event_name=event_name,
                day=_bounded(entry.get("day"), "question day", 0, 100_000),
                minute=_bounded(entry.get("minute"), "question minute", 0, 1439),
                round_number=_bounded(
                    entry.get("round_number"), "question round_number", 0, 100_000
                ),
            )
        )
    return PossibleTruthsConfig(
        questions=questions,
        max_answer_chars=_bounded(raw.get("max_answer_chars"), "max_answer_chars", 500, 10_000),
        require_declarative=_parse_bool(
            raw.get("require_declarative"), "require_declarative", True
        ),
        feed_dm=_parse_bool(raw.get("feed_dm"), "feed_dm", True),
    )


def parse_plugins(value: object) -> list[RoomPlugin]:
    """Validate the host-supplied room plugins."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("'plugins' 必须是插件对象列表")
    if len(value) > MAX_PLUGINS:
        raise ValueError(f"最多允许 {MAX_PLUGINS} 个插件")
    plugins: list[RoomPlugin] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("每个插件必须是含 'type' 的对象")
        plugin_type = entry.get("type")
        if plugin_type != "possible_truths":
            raise ValueError(f"未知的插件类型 '{plugin_type}'。")
        if plugin_type in seen:
            raise ValueError("同一插件类型只能出现一次。")
        seen.add(plugin_type)
        config = _parse_truths_config(entry.get("config") or {})
        plugins.append(
            RoomPlugin(
                type=plugin_type,
                enabled=_parse_bool(entry.get("enabled"), "plugin enabled", True),
                config=config.model_dump(),
            )
        )
    return plugins


def truths_config(plugins: list[RoomPlugin]) -> PossibleTruthsConfig | None:
    """Return the enabled possible-truths config, if any."""
    for plugin in plugins:
        if plugin.type == "possible_truths" and plugin.enabled:
            return PossibleTruthsConfig(**plugin.config)
    return None


def newly_revealed(
    config: PossibleTruthsConfig,
    revealed: set[str],
    *,
    current_round: int,
    time_total: int | None,
    fired_events: set[str],
) -> list[TruthQuestion]:
    """Return questions whose reveal conditions are now satisfied."""
    result: list[TruthQuestion] = []
    for question in config.questions:
        if question.id in revealed:
            continue
        if question.condition == "start":
            satisfied = True
        elif question.condition == "round":
            satisfied = current_round >= question.round_number
        elif question.condition == "time":
            satisfied = (
                time_total is not None and time_total >= question.day * 1440 + question.minute
            )
        else:
            satisfied = question.event_name in fired_events
        if satisfied:
            result.append(question)
    return result

"""What one player-visible connection knows, rebuilt only from its own events.

Each seat keeps its own ``PublicView``: the opening, its own actions, the rounds
it has seen and the public map/codex. Nothing private ever enters here, because
nothing private is ever sent to a player socket.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

from playtest import protocol as P
from playtest.protocol import Event

DEFAULT_DIGEST_ROUNDS = 3
DEFAULT_DIGEST_LIMIT = 260
OPENING_LIMIT = 300
SCENE_LIMIT = 240
NARRATIVE_LIMIT = 260
OWN_ACTION_LIMIT = 120
OTHER_LIMIT = 140
MAX_OTHERS_PER_ROUND = 4
MAX_DIGEST_CHARS = 3200
MAX_NOTICES = 20


def _truncate(text: str, limit: int) -> str:
    """Collapse whitespace and cut to a bounded length."""
    collapsed = " ".join(text.split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: max(0, limit - 1)] + "…"


@dataclass
class RoundRecord:
    """One resolved round as this player saw it."""

    number: int
    title: str = ""
    narrative: str = ""
    outcomes: dict[str, str] = field(default_factory=dict)
    dice: dict[str, int] = field(default_factory=dict)
    game_time: str = ""


@dataclass
class PublicView:
    """The player-facing state of one connection."""

    client_id: str
    name: str = ""
    character: str | None = None
    is_host: bool = False
    title: str = ""
    opening: str = ""
    state: str = ""
    schedule_mode: str = "round"
    game_time: str = ""
    player_order: list[str] = field(default_factory=list)
    roster: list[dict[str, Any]] = field(default_factory=list)
    rounds: list[RoundRecord] = field(default_factory=list)
    my_actions: dict[int, str] = field(default_factory=dict)
    round_actions: dict[int, dict[str, str]] = field(default_factory=dict)
    pending_proposals: list[dict[str, Any]] = field(default_factory=list)
    agreed_plans: list[dict[str, Any]] = field(default_factory=list)
    active_player_ids: list[str] = field(default_factory=list)
    waiting: list[dict[str, Any]] = field(default_factory=list)
    scene_notes: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    map_state: dict[str, Any] | None = None
    codex_state: dict[str, Any] | None = None
    truth_questions: list[dict[str, Any]] = field(default_factory=list)
    has_map: bool = False
    has_codex: bool = False
    truth_enabled: bool = False
    truth_used_round: int | None = None
    truth_max_chars: int = 500
    closed_reason: str = ""

    @property
    def round_number(self) -> int:
        """Return the latest round number this view has seen."""
        return self.rounds[-1].number if self.rounds else 0

    @property
    def is_my_turn(self) -> bool:
        """Return whether this client may act right now."""
        return self.client_id in self.active_player_ids

    def apply(self, event: Event) -> None:
        """Fold one server event into the view."""
        handler = getattr(self, f"_on_{event.type}", None)
        if handler is not None:
            handler(event)

    def note(self, message: str) -> None:
        """Keep a bounded list of system notices."""
        if not message:
            return
        self.notices.append(message)
        del self.notices[:-MAX_NOTICES]

    # -- event handlers -------------------------------------------------
    def _on_auth_ok(self, event: Event) -> None:
        self.name = event.text("name", self.name)
        character = event.text("character")
        self.character = character or self.character
        self.is_host = bool(event.payload.get("is_host", self.is_host))
        self.title = event.text("scenario_title") or self.title
        opening = event.text("opening_scenario")
        if opening:
            self.opening = opening
        self.state = event.text("state", self.state)
        self.schedule_mode = event.text("schedule_mode", self.schedule_mode) or "round"
        self.game_time = event.text("game_time", self.game_time)
        self.has_map = bool(event.payload.get("has_map", self.has_map))
        self.has_codex = bool(event.payload.get("has_codex", self.has_codex))
        order = event.listing("player_order")
        if order:
            self.player_order = [str(item) for item in order]
        self.roster = [item for item in event.listing("players") if isinstance(item, dict)]
        proposals = event.listing("pending_proposals")
        if proposals:
            self.pending_proposals = [item for item in proposals if isinstance(item, dict)]
        plans = event.listing("agreed_plans")
        if plans:
            self.agreed_plans = [item for item in plans if isinstance(item, dict)]
        self._record_round(event)
        self._record_truth(event)

    def _on_scenario_ready(self, event: Event) -> None:
        self.title = event.text("title", self.title)
        self.state = "AWAITING_PLAYERS"

    def _on_state_update(self, event: Event) -> None:
        number = event.number("round_number")
        if not number:
            # The start-of-game broadcast carries the opening, not a resolved round.
            opening = event.text("global_narrative")
            if opening and not self.opening:
                self.opening = opening
            title = event.text("round_title")
            if title and not self.title:
                self.title = title
            self.game_time = event.text("game_time", self.game_time)
            return
        record = RoundRecord(
            number=number,
            title=event.text("round_title"),
            narrative=event.text("global_narrative"),
            outcomes={
                str(key): str(value) for key, value in event.mapping("player_resolutions").items()
            },
            dice={
                str(key): int(value)
                for key, value in event.mapping("dice_results").items()
                if isinstance(value, int) and not isinstance(value, bool)
            },
            game_time=event.text("game_time"),
        )
        self.rounds = [item for item in self.rounds if item.number != number]
        self.rounds.append(record)
        self.rounds.sort(key=lambda item: item.number)
        self.state = "ACTIVE_TURN"
        self.active_player_ids = []
        self.waiting = []
        if record.game_time:
            self.game_time = record.game_time
        order = event.listing("player_order")
        if order:
            self.player_order = [str(item) for item in order]

    def _on_round_start(self, event: Event) -> None:
        del event

    def _on_action_echo(self, event: Event) -> None:
        number = event.number("round_number") or self.round_number + 1
        actor = event.text("player_name")
        action = event.text("action")
        if actor:
            # Every declared action is public in the game log; a seat may respond to it.
            self.round_actions.setdefault(number, {})[actor] = action
        if actor and actor == self.character:
            self.my_actions[number] = action

    def _on_turn_directive(self, event: Event) -> None:
        active = event.text("active_player_id")
        self.active_player_ids = [active] if active else []
        self.waiting = []
        number = event.number("round_number")
        submitted = event.mapping("submitted_actions")
        if number and submitted:
            self.round_actions.setdefault(number, {}).update(
                {str(name): str(text) for name, text in submitted.items()}
            )
        order = event.listing("player_order")
        if order:
            self.player_order = [str(item) for item in order]

    def _on_turn_window(self, event: Event) -> None:
        self.active_player_ids = [str(item) for item in event.listing("active_player_ids")]
        self.waiting = [item for item in event.listing("waiting") if isinstance(item, dict)]
        self.game_time = event.text("game_time", self.game_time)
        number = event.number("round_number")
        if number:
            for item in self.waiting:
                name = str(item.get("name") or "")
                action = str(item.get("action") or "")
                if name and action:
                    self.round_actions.setdefault(number, {})[name] = action

    def _on_scene_text(self, event: Event) -> None:
        text = event.text("text")
        if text:
            self.scene_notes.append(_truncate(text, DEFAULT_DIGEST_LIMIT))

    def _on_map_state(self, event: Event) -> None:
        self.map_state = dict(event.payload)

    def _on_codex_state(self, event: Event) -> None:
        self.codex_state = dict(event.payload)

    def _on_truth_state(self, event: Event) -> None:
        self.truth_enabled = bool(event.payload.get("enabled", self.truth_enabled))
        used = event.payload.get("your_used_round")
        self.truth_used_round = (
            used if isinstance(used, int) and not isinstance(used, bool) else None
        )
        max_chars = event.payload.get("max_answer_chars")
        if isinstance(max_chars, int) and not isinstance(max_chars, bool):
            self.truth_max_chars = max(1, max_chars)
        self._record_truth(event)

    def _on_truth_question(self, event: Event) -> None:
        question_id = event.text("id")
        if question_id and not any(
            str(item.get("id")) == question_id for item in self.truth_questions
        ):
            self.truth_questions.append(
                {"id": question_id, "text": event.text("text"), "history": []}
            )

    def _on_player_roster(self, event: Event) -> None:
        self.roster = [
            item
            for key in ("players", "characters")
            for item in event.listing(key)
            if isinstance(item, dict)
        ] or self.roster

    def _on_system_msg(self, event: Event) -> None:
        self.note(event.text("msg"))

    def _on_error(self, event: Event) -> None:
        message = event.text("msg") or "未知错误"
        self.errors.append(message)
        del self.errors[:-MAX_NOTICES]

    def _on_game_ended(self, event: Event) -> None:
        self.state = "ENDED"
        self.closed_reason = event.text("msg") or "游戏已结束。"

    def _on_room_closed(self, event: Event) -> None:
        self.state = "CLOSED"
        self.closed_reason = event.text("msg") or "房间已关闭。"

    def _on_removed(self, event: Event) -> None:
        self.state = "REMOVED"
        self.closed_reason = event.text("msg") or "已被移出游戏。"

    def _on_truth_answer(self, event: Event) -> None:
        question_id = event.text("question_id")
        for question in self.truth_questions:
            if str(question.get("id")) == question_id:
                history = question.setdefault("history", [])
                if isinstance(history, list):
                    history.append(
                        {"character": event.text("character"), "text": event.text("text")}
                    )

    # -- helpers --------------------------------------------------------
    def _record_round(self, event: Event) -> None:
        """Fold the snapshot's own history into rounds when it is available."""
        history = event.listing("round_history")
        for entry in history:
            if not isinstance(entry, Mapping):
                continue
            number = entry.get("round_number")
            if isinstance(number, bool) or not isinstance(number, int):
                continue
            if any(item.number == number for item in self.rounds):
                continue
            self.rounds.append(
                RoundRecord(
                    number=number,
                    title=str(entry.get("round_title") or ""),
                    narrative=str(entry.get("global_narrative") or ""),
                    outcomes={
                        str(key): str(value)
                        for key, value in (entry.get("player_resolutions") or {}).items()
                    },
                    game_time=str(entry.get("game_time") or ""),
                )
            )
        self.rounds.sort(key=lambda item: item.number)

    def _record_truth(self, event: Event) -> None:
        """Fold the possible-truths snapshot into the question list."""
        truth = event.mapping("truth")
        questions = truth.get("questions")
        if not isinstance(questions, list):
            return
        for item in questions:
            if not isinstance(item, Mapping):
                continue
            question_id = str(item.get("id") or "")
            if not question_id or any(
                str(known.get("id")) == question_id for known in self.truth_questions
            ):
                continue
            self.truth_questions.append(
                {
                    "id": question_id,
                    "text": str(item.get("text") or ""),
                    "history": list(item.get("history") or []),
                }
            )

    def digest(
        self,
        *,
        rounds: int = DEFAULT_DIGEST_ROUNDS,
        limit: int = DEFAULT_DIGEST_LIMIT,
    ) -> str:
        """Render a bounded, player-visible briefing for one seat prompt.

        The sections a player actually acts on (this round's declared actions,
        the map, the turn state) come last and stay whole; the DM's prose is
        trimmed first, because a wall of narration is what pushes a seat into
        writing novel prose instead of a game action.
        """
        head: list[str] = []
        header = self.title or "未命名剧本"
        if self.game_time:
            header += f"（游戏内时间 {self.game_time}）"
        head.append(f"【剧本】{header}")
        head.append(
            "【以下是 DM 写的已发生事件，仅供你了解情况：不要模仿这种叙述口吻，也不要复述】"
        )
        if self.opening and not self.rounds:
            head.append(f"【开场】{_truncate(self.opening, OPENING_LIMIT)}")
        if self.scene_notes:
            head.append("【场景】" + _truncate(self.scene_notes[-1], SCENE_LIMIT))
        recent = self.rounds[-rounds:] if rounds > 0 else list(self.rounds)
        while recent:
            head.append("【最近回合】")
            for record in recent:
                title = f" {record.title}" if record.title else ""
                head.append(
                    f"第{record.number}回合{title}：{_truncate(record.narrative, NARRATIVE_LIMIT)}"
                )
                mine = self.my_actions.get(record.number)
                if mine:
                    head.append(f"  · 你上一轮说的/做的：{_truncate(mine, OWN_ACTION_LIMIT)}")
                outcome = record.outcomes.get(self.character or "")
                if outcome:
                    head.append(f"  · 你那轮的结果（DM 写）：{_truncate(outcome, limit)}")
                declared = self.round_actions.get(record.number) or {}
                others = [
                    f"{name}：{_truncate(text, OTHER_LIMIT)}"
                    for name, text in list(declared.items())[:MAX_OTHERS_PER_ROUND]
                    if name != self.character
                ] or [
                    f"{name}：{_truncate(text, OTHER_LIMIT)}"
                    for name, text in list(record.outcomes.items())[:MAX_OTHERS_PER_ROUND]
                    if name != self.character
                ]
                if others:
                    head.append("  · 其他人：")
                    head[-1] += "；".join(others)
            if sum(len(line) for line in head) <= MAX_DIGEST_CHARS or len(recent) == 1:
                break
            recent = recent[1:]  # drop the oldest round first
        tail: list[str] = []
        if self.map_state:
            tail.append("【地图】" + self.map_brief())
        if self.codex_state:
            tail.append("【图鉴】" + self.codex_brief())
        truth = self.truth_brief()
        if truth:
            tail.append("【可能的真相】" + truth)
        if self.pending_proposals:
            tail.append(
                "【等你回应的提议】"
                + "；".join(
                    f"{item.get('from')}：{_truncate(str(item.get('content') or ''), 120)}"
                    for item in self.pending_proposals[:3]
                    if isinstance(item, Mapping)
                )
                + "（你的行动里要说清你在回应谁、答应还是拒绝）"
            )
        if self.agreed_plans:
            tail.append(
                "【已说好的计划】"
                + "；".join(
                    _truncate(str(item.get("content") or ""), 120)
                    for item in self.agreed_plans[:3]
                    if isinstance(item, Mapping)
                )
            )
        declared_now = self.declared_actions()
        if declared_now:
            tail.append(
                "【本轮其他人已经声明的行动（同时发生，供你回应；不要替他们写结果）】"
                + "；".join(
                    f"{name}：{_truncate(text, OTHER_LIMIT)}" for name, text in declared_now.items()
                )
            )
        waiting = self.waiting_brief()
        if waiting:
            tail.append("【还在进行中】" + waiting)
        tail.append("【当前】" + self.turn_brief())
        return "\n".join(head + tail)

    def declared_actions(self) -> dict[str, str]:
        """Return the actions other players already declared this round."""
        for number in sorted(self.round_actions, reverse=True):
            actions = {
                name: text
                for name, text in self.round_actions[number].items()
                if name != self.character and text
            }
            if actions:
                return actions
        return {}

    def waiting_brief(self) -> str:
        """Describe who is still busy, with whatever action text is public."""
        parts = []
        for item in self.waiting[:5]:
            if not isinstance(item, Mapping):
                continue
            name = str(item.get("name") or "?")
            action = str(item.get("action") or "")
            eta = str(item.get("ends_at_label") or "")
            detail = f"{name}：{_truncate(action, 60)}" if action else f"{name}还在观望"
            if eta:
                detail += f"（预计 {eta} 完成）"
            parts.append(detail)
        return "；".join(parts)

    def turn_brief(self) -> str:
        """Describe whose turn it is in one short line."""
        if self.closed_reason:
            return self.closed_reason
        if self.is_my_turn:
            who = f"轮到你了（{self.character}）"
        elif self.active_player_ids:
            who = "等待其他玩家行动：" + "、".join(self.active_player_ids)
        else:
            who = "等待 DM 结算"
        if self.player_order:
            who += "；队伍：" + "、".join(self.player_order)
        return who

    def map_brief(self) -> str:
        """Describe visited regions, open paths and the party, compactly.

        Mirrors the server's map payload: visited regions only, with the items
        still lying there, the paths that are open and the ones that are gated.
        """
        state = self.map_state or {}
        regions = [item for item in state.get("regions") or [] if isinstance(item, Mapping)]
        if not regions:
            return "尚未探索任何地区。"
        names = {
            str(region.get("id")): str(region.get("name") or region.get("id")) for region in regions
        }
        parts: list[str] = []
        for region in regions:
            items = [
                str(item.get("name")) + ("（已查看）" if item.get("investigated") else "")
                for item in region.get("items") or []
                if isinstance(item, Mapping)
            ]
            reachable = [
                str(target.get("name") or target.get("id"))
                for target in region.get("reachable") or []
                if isinstance(target, Mapping)
            ]
            locked = [
                str(target.get("name") or target.get("id"))
                for target in region.get("locked") or []
                if isinstance(target, Mapping)
            ]
            detail = f"{region.get('name') or region.get('id')}"
            if items:
                detail += f"：可见物品 {'、'.join(items)}"
            if reachable:
                detail += f"；可前往 {'、'.join(reachable)}"
            if locked:
                detail += f"；受阻 {'、'.join(locked)}"
            parts.append(detail)
        where: list[str] = []
        for player in state.get("players") or []:
            if not isinstance(player, Mapping):
                continue
            location = names.get(str(player.get("region")), str(player.get("region") or "?"))
            carried = [str(item) for item in player.get("carried") or []]
            detail = f"{player.get('name')}在{location}"
            if player.get("in_transit_to"):
                detail += f"（正前往 {player['in_transit_to']}）"
            if carried:
                detail += f"（携带 {'、'.join(carried)}）"
            where.append(detail)
        text = "；".join(parts)
        if where:
            text += "｜队伍位置：" + "，".join(where)
        return text

    def codex_brief(self) -> str:
        """Describe discovered items and fired events, compactly."""
        state = self.codex_state or {}
        items = [item for item in state.get("items") or [] if isinstance(item, Mapping)]
        events = [entry for entry in state.get("events") or [] if isinstance(entry, Mapping)]
        parts: list[str] = []
        if items:
            parts.append(
                "已知物品："
                + "、".join(
                    str(item.get("name"))
                    + ("（线索）" if item.get("clue") else "")
                    + ("［已查看］" if item.get("investigated") else "")
                    for item in items[:12]
                )
            )
        if events:
            parts.append("已发生事件：" + "、".join(str(entry.get("name")) for entry in events[:8]))
        return "；".join(parts) if parts else "尚无图鉴记录。"

    def truth_brief(self) -> str:
        """Describe revealed possible-truth questions and their answers so far."""
        if not self.truth_questions:
            return ""
        parts = []
        for question in self.truth_questions[:6]:
            text = _truncate(str(question.get("text") or ""), 120)
            answers = [
                f"{entry.get('character')}：{_truncate(str(entry.get('text') or ''), 80)}"
                for entry in question.get("history") or []
                if isinstance(entry, Mapping)
            ]
            detail = f"{question.get('id')}｜{text}"
            if answers:
                detail += "｜已有陈述：" + "；".join(answers)
            parts.append(detail)
        return "；".join(parts)

    def latest_own_action(self) -> str:
        """Return this player's most recent submitted action."""
        if not self.my_actions:
            return ""
        latest = max(self.my_actions)
        return self.my_actions[latest]


def is_actionable(event: Event, client_id: str) -> bool:
    """Return whether an event hands the turn to this client."""
    if event.type == P.TURN_DIRECTIVE:
        return event.text("active_player_id") == client_id
    if event.type == P.TURN_WINDOW:
        return client_id in [str(item) for item in event.listing("active_player_ids")]
    return False

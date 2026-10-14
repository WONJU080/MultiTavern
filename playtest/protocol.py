"""Client-side wire protocol: envelope builders and server-event parsing.

The playtest driver speaks exactly the envelopes a browser sends, so a card
that plays here plays for real. Keep the builders pure: no state, no I/O.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Mapping

# Client event types the driver uses today.
CREATE_ROOM = "create_room"
JOIN_ROOM = "join_room"
SCENARIO_INIT = "scenario_init"
START_GAME = "start_game"
ACTION = "action"
HOLD = "hold"
SKIP_VOTE = "skip_vote"
END_GAME = "end_game"
CLOSE_ROOM = "close_room"
RETRY_ROUND = "retry_round"
MAP_REQUEST = "map_request"
CODEX_REQUEST = "codex_request"
TRUTH_REQUEST = "truth_request"
TRUTH_SUBMIT = "truth_submit"
CHAT = "chat"

# Server events the driver reacts to.
AUTH_OK = "auth_ok"
SCENARIO_READY = "scenario_ready"
TURN_DIRECTIVE = "turn_directive"
TURN_WINDOW = "turn_window"
ROUND_START = "round_start"
SCENE_TEXT = "scene_text"
STATE_UPDATE = "state_update"
ACTION_ECHO = "action_echo"
PLAYER_ROSTER = "player_roster"
DM_THINKING = "dm_thinking"
TOKEN_USAGE = "token_usage"
MAP_STATE = "map_state"
CODEX_STATE = "codex_state"
TRUTH_STATE = "truth_state"
TRUTH_QUESTION = "truth_question"
TRUTH_RESULT = "truth_result"
TRUTH_ANSWER = "truth_answer"
GAME_ENDED = "game_ended"
ROOM_CLOSED = "room_closed"
REMOVED = "removed"
SYSTEM_MSG = "system_msg"
CHAT_ECHO = "chat_echo"
ERROR = "error"
KEY_UPDATED = "key_updated"


class ProtocolError(RuntimeError):
    """Raised when the server sends something the client cannot interpret."""


@dataclass(frozen=True, slots=True)
class Event:
    """One decoded server event."""

    type: str
    payload: Mapping[str, Any] = field(default_factory=dict)

    @classmethod
    def from_wire(cls, raw: Any) -> "Event":
        """Decode one JSON message, rejecting anything malformed."""
        if not isinstance(raw, dict):
            raise ProtocolError(f"服务器消息不是对象：{type(raw).__name__}")
        event_type = raw.get("type")
        if not isinstance(event_type, str) or not event_type:
            raise ProtocolError("服务器消息缺少 type。")
        payload = raw.get("payload")
        if payload is None:
            payload = {}
        if not isinstance(payload, dict):
            raise ProtocolError(f"{event_type} 的 payload 不是对象。")
        return cls(type=event_type, payload=payload)

    def text(self, key: str, default: str = "") -> str:
        """Return a payload string field, tolerating non-strings."""
        value = self.payload.get(key, default)
        return value if isinstance(value, str) else default

    def number(self, key: str) -> int | None:
        """Return a payload integer field when it is a plain integer."""
        value = self.payload.get(key)
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        return value

    def mapping(self, key: str) -> dict[str, Any]:
        """Return a payload object field, or an empty mapping."""
        value = self.payload.get(key)
        return dict(value) if isinstance(value, dict) else {}

    def listing(self, key: str) -> list[Any]:
        """Return a payload list field, or an empty list."""
        value = self.payload.get(key)
        return list(value) if isinstance(value, list) else []


def envelope(event_type: str, data: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build one client envelope."""
    return {"event_type": event_type, "data": dict(data or {})}


def create_room(name: str, *, credential: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Build create_room, optionally with a host-supplied DM credential."""
    data: dict[str, Any] = {"name": name}
    if credential is not None:
        data["llm"] = dict(credential)
    return envelope(CREATE_ROOM, data)


def join_room(
    name: str,
    invite_code: str,
    character: str | None = None,
    *,
    reconnect_token: str | None = None,
) -> dict[str, Any]:
    """Build join_room for a seat claiming one character."""
    data: dict[str, Any] = {"name": name, "invite_code": invite_code}
    if character:
        data["character"] = character
    if reconnect_token:
        data["reconnect_token"] = reconnect_token
    return envelope(JOIN_ROOM, data)


def scenario_init(payload: Mapping[str, Any]) -> dict[str, Any]:
    """Build scenario_init from a card payload."""
    return envelope(SCENARIO_INIT, payload)


def start_game() -> dict[str, Any]:
    """Build start_game for the host."""
    return envelope(START_GAME, {})


def action(text: str) -> dict[str, Any]:
    """Build one player action."""
    return envelope(ACTION, {"action": text})


def hold() -> dict[str, Any]:
    """Build a strict-mode hold (observe without consuming in-game time)."""
    return envelope(HOLD, {})


def skip_vote(target: str | None = None) -> dict[str, Any]:
    """Build a skip vote, targeting a player id in strict mode."""
    data: dict[str, Any] = {}
    if target:
        data["target"] = target
    return envelope(SKIP_VOTE, data)


def end_game() -> dict[str, Any]:
    """Build end_game for the host."""
    return envelope(END_GAME, {})


def close_room() -> dict[str, Any]:
    """Build close_room for the host."""
    return envelope(CLOSE_ROOM, {})


def retry_round() -> dict[str, Any]:
    """Build retry_round for the host after a paused inference failure."""
    return envelope(RETRY_ROUND, {})


def map_request() -> dict[str, Any]:
    """Build a request for the shared map snapshot."""
    return envelope(MAP_REQUEST, {})


def codex_request() -> dict[str, Any]:
    """Build a request for the discovered-items codex."""
    return envelope(CODEX_REQUEST, {})


def truth_request() -> dict[str, Any]:
    """Build a request for the possible-truths state."""
    return envelope(TRUTH_REQUEST, {})


def truth_submit(question_id: str, answer: str) -> dict[str, Any]:
    """Build one possible-truth submission."""
    return envelope(TRUTH_SUBMIT, {"question_id": question_id, "answer": answer})

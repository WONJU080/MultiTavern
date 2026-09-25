"""Strict WebSocket and LLM data contracts."""

from typing import Any, Literal

from pydantic import BaseModel, ConfigDict, Field


class StrictModel(BaseModel):
    """Base model that forbids coercion and unknown fields."""

    model_config = ConfigDict(strict=True, extra="forbid")


class ClientPayload(StrictModel):
    """Envelope for a client-to-server WebSocket message."""

    event_type: Literal[
        "create_room",
        "join_room",
        "room_info",
        "chat",
        "action",
        "skip_vote",
        "scenario_init",
        "start_game",
        "end_game",
        "close_room",
        "retry_round",
        "history_request",
        "character_update",
    ]
    data: dict[str, Any]


class ServerEvent(StrictModel):
    """Envelope for a server-to-client WebSocket message."""

    type: Literal[
        "state_update",
        "chat_echo",
        "turn_directive",
        "error",
        "system_msg",
        "auth_ok",
        "scenario_ready",
        "round_start",
        "action_echo",
        "player_roster",
        "dm_thinking",
        "game_ended",
        "room_closed",
        "room_info",
        "skip_vote",
        "removed",
        "token_usage",
        "history_chunk",
    ]
    payload: dict[str, Any]


class Character(StrictModel):
    """A cast member defined by the host; claimed by one player at a time."""

    name: str
    description: str = ""
    personality: str = ""
    style: str = ""
    example_dialogue: str = ""


class DicePlan(StrictModel):
    """LLM-selected checks; hidden names are never disclosed to clients."""

    rolls: dict[str, bool]
    hidden_rolls: list[str]


class ContextSummary(StrictModel):
    """Structured memory retained after older round history is compacted."""

    world_state: str
    player_states: dict[str, str]
    important_npcs: str
    unresolved_threads: list[str]


class RoundResolution(StrictModel):
    """Structured outcome of a resolved round."""

    round_title: str | None = None
    global_narrative: str
    player_resolutions: dict[str, str]
    time_elapsed_minutes: int | None = Field(default=None, ge=0)


class TimeRule(StrictModel):
    """A reference duration for one kind of in-world activity."""

    activity: str
    minutes_min: int = Field(default=0, ge=0)
    minutes_max: int = Field(default=60, ge=0)


class TimeConfig(StrictModel):
    """Authoritative in-game clock settings supplied by the host."""

    enabled: bool = False
    start_day: int = Field(default=1, ge=0)
    start_minute: int = Field(default=390, ge=0, lt=1440)
    max_elapsed_minutes: int = Field(default=600, ge=0)
    default_elapsed_minutes: int = Field(default=15, ge=0)


class TimedEvent(StrictModel):
    """A scheduled story event that fires at a fixed in-game time."""

    name: str
    day: int = Field(default=0, ge=0)
    minute: int = Field(default=0, ge=0, lt=1440)
    description: str
    public: bool = False


class LorebookEntry(StrictModel):
    """A world-info entry activated by keyword matches in recent play."""

    title: str = ""
    keys: list[str]
    content: str
    order: int = Field(default=0, ge=0, le=10_000)
    enabled: bool = True
    constant: bool = False


class PromptBlock(StrictModel):
    """A host-supplied instruction block placed at a fixed prompt position."""

    title: str = ""
    content: str
    position: Literal["system", "scenario", "output"] = "output"
    enabled: bool = True


class SamplingConfig(StrictModel):
    """Per-room generation sampling overrides for supported provider fields."""

    temperature: float | None = Field(default=None, ge=0, le=2)
    top_p: float | None = Field(default=None, ge=0, le=1)


class ScenarioTitle(StrictModel):
    """Title-only preparation before the party has joined."""

    title: str


class SummaryAudit(StrictModel):
    """Private check of a proposed memory checkpoint against its source context."""

    preserved: bool
    corrections: list[str]

"""Internal game domain types and dependency protocols."""

from dataclasses import dataclass, field
import secrets
from enum import Enum, auto
from typing import Any, Protocol

from core.schemas import Character, DicePlan, RoundResolution, ServerEvent


class GameState(Enum):
    """States of the game session lifecycle."""

    AWAITING_HOST = auto()
    AWAITING_PLAYERS = auto()
    SCENARIO_INJECTION = auto()
    ACTIVE_TURN = auto()
    AWAITING_LLM = auto()
    ENDED = auto()


@dataclass(slots=True)
class Player:
    """A participant in the session, tracked across disconnects and returns."""

    client_id: str
    name: str
    is_host: bool
    join_index: int = 0
    character_name: str | None = None
    is_connected: bool = True
    departure_pending: bool = False
    return_pending: bool = False
    handover_pending: bool = False
    skip_pending: bool = False
    connection_version: int = 0
    reconnect_token: str = field(default_factory=lambda: secrets.token_urlsafe(32))
    last_seen_total: int | None = None
    catchup_notes: list[str] = field(default_factory=list)


class EventSender(Protocol):
    """Protocol for broadcasting and personal server events."""

    async def broadcast_global(self, event: ServerEvent) -> None:
        """Broadcast an event to all active connections."""
        ...

    async def send_personal(self, client_id: str, event: ServerEvent) -> None:
        """Send an event to a single client."""
        ...

    async def broadcast_except(self, client_id: str, event: ServerEvent) -> None:
        """Broadcast an event to all connections except one client."""
        ...


class ResolutionManager(Protocol):
    """Protocol for the LLM resolution backend."""

    def begin_round_usage(self, number: int) -> None:
        """Group inference consumption, retaining totals across host retries."""
        ...

    def usage_snapshot(self) -> dict[str, Any]:
        """Return prompt-free round/game totals and estimated context occupancy."""
        ...

    def finish_round_usage(self, error: str | None = None) -> None:
        """Finish timing round work, excluding the human wait before a retry."""
        ...

    def set_genesis(
        self, scenario: str, guidance: str = "", time_context: str | None = None
    ) -> None:
        """Set the initial scenario, optional guidance, and rendered time rules."""
        ...

    def set_cast(self, cast: list[Any]) -> None:
        """Set the cast character cards used in the fixed context."""
        ...

    def set_lorebook(self, entries: list[Any]) -> None:
        """Set the world-book entries used for keyword-triggered insertion."""
        ...

    def set_prompt_blocks(self, blocks: list[Any]) -> None:
        """Set the ordered instruction blocks injected at fixed positions."""
        ...

    def set_sampling(self, config: Any) -> None:
        """Set optional per-room sampling overrides."""
        ...

    async def discover_context_window(self) -> None:
        """Discover the backend context window size."""
        ...

    async def generate_scenario_title(self) -> str:
        """Generate only the scenario title before players join."""
        ...

    async def generate_start_state(
        self, cast: list[Character], claims: dict[str, str], current_time: str = ""
    ) -> RoundResolution:
        """Introduce the cast and its player-controlled characters at game start."""
        ...

    async def plan_dice(self, round_buffer: dict[str, str], current_state: str = "") -> DicePlan:
        """Plan which actions need a d100 check."""
        ...

    async def generate_resolution(
        self,
        round_buffer: dict[str, str],
        dice_results: dict[str, int] | None = None,
        hidden_rolls: set[str] | None = None,
        current_time: str = "",
        event_context: str = "",
    ) -> RoundResolution:
        """Resolve a round of actions."""
        ...

    async def preflight_round(self, actions: dict[str, str], current_state: str = "") -> None:
        """Reject actions that exceed the context budget."""
        ...

    async def close(self) -> None:
        """Release backend resources."""
        ...

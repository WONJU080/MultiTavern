"""Authentication, scenario creation, chat, and lobby transitions."""

import hashlib
import hmac
import logging
from collections import deque
from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING

from core.config import settings
from core.schemas import (
    Character,
    ClientPayload,
    LorebookEntry,
    PromptBlock,
    SamplingConfig,
    ServerEvent,
    TimeConfig,
    TimedEvent,
    TimeRule,
)
from logic.game_clock import GameClock
from logic.models import GameState, Player
from logic.validation import clean_optional_text, clean_text

if TYPE_CHECKING:
    from logic.engine import GameEngine

LOGGER = logging.getLogger(__name__)
CURRENT_OWNER: ContextVar[Callable[[], bool]] = ContextVar("socket_owner", default=lambda: True)


def parse_cast(value: object) -> list[Character]:
    """Validate the host-supplied cast of characters."""
    if not isinstance(value, list) or not value:
        raise ValueError("'characters' must be a non-empty list")
    if len(value) > settings.server.max_characters:
        raise ValueError(f"At most {settings.server.max_characters} characters are allowed.")
    cast: list[Character] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Each character must be an object with 'name' and 'description'")
        char_name = clean_text(entry.get("name"), "character name", 40)
        description = clean_optional_text(entry.get("description"), "character description", 50_000)
        personality = clean_optional_text(entry.get("personality"), "personality", 10_000)
        style = clean_optional_text(entry.get("style"), "style", 10_000)
        example_dialogue = clean_optional_text(
            entry.get("example_dialogue"), "example_dialogue", 10_000
        )
        folded = char_name.casefold()
        if folded in seen:
            raise ValueError("Character names must be unique.")
        seen.add(folded)
        cast.append(
            Character(
                name=char_name,
                description=description,
                personality=personality,
                style=style,
                example_dialogue=example_dialogue,
            )
        )
    return cast


def _bounded_int(value: object, field: str, default: int, maximum: int) -> int:
    """Return an integer within [0, maximum], or the default for None/missing."""
    if value is None:
        return default
    if isinstance(value, bool) or not isinstance(value, int):
        raise ValueError(f"'{field}' must be an integer")
    if value < 0 or value > maximum:
        raise ValueError(f"'{field}' must be between 0 and {maximum}")
    return value


def parse_time_config(value: object) -> TimeConfig:
    """Validate the host-supplied in-game clock configuration."""
    if value is None:
        return TimeConfig()
    if not isinstance(value, dict):
        raise ValueError("'time_config' must be an object")
    if not isinstance(value.get("enabled", False), bool):
        raise ValueError("'time_config.enabled' must be a boolean")
    return TimeConfig(
        enabled=bool(value.get("enabled", False)),
        start_day=_bounded_int(value.get("start_day"), "time_config.start_day", 1, 100_000),
        start_minute=_bounded_int(value.get("start_minute"), "time_config.start_minute", 390, 1439),
        max_elapsed_minutes=_bounded_int(
            value.get("max_elapsed_minutes"), "time_config.max_elapsed_minutes", 600, 525_600
        ),
        default_elapsed_minutes=_bounded_int(
            value.get("default_elapsed_minutes"), "time_config.default_elapsed_minutes", 15, 525_600
        ),
    )


def parse_time_rules(value: object) -> list[TimeRule]:
    """Validate the host-supplied reference durations for activities."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("'time_rules' must be a list of activity objects")
    if len(value) > 50:
        raise ValueError("At most 50 time rules are allowed")
    rules: list[TimeRule] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Each time rule must be an object with 'activity'")
        activity = clean_text(entry.get("activity"), "time rule activity", 80)
        minimum = _bounded_int(entry.get("minutes_min"), "minutes_min", 0, 525_600)
        maximum = _bounded_int(entry.get("minutes_max"), "minutes_max", 60, 525_600)
        if minimum > maximum:
            raise ValueError(f"Time rule '{activity}': minutes_min exceeds minutes_max")
        rules.append(TimeRule(activity=activity, minutes_min=minimum, minutes_max=maximum))
    return rules


def render_time_block(config: TimeConfig, rules: list[TimeRule]) -> str:
    """Render the authoritative clock rules into a stable backstage prompt block."""
    if not config.enabled:
        return ""
    start = f"Day {config.start_day} {config.start_minute // 60:02d}:{config.start_minute % 60:02d}"
    lines = [
        "Time rules (authoritative):",
        f"- The story starts at {start} and runs on an in-game clock.",
        "- Every round's request states the authoritative current time; always use it.",
        "- Estimate the in-world minutes each round's actions take in time_elapsed_minutes.",
        "- Parallel actions on different lines count only the longest line, not the sum.",
        f"- Durations above {config.max_elapsed_minutes} minutes are not allowed.",
    ]
    if rules:
        lines.append("- Reference durations:")
        lines.extend(
            f"  - {rule.activity}: {rule.minutes_min}-{rule.minutes_max} minutes" for rule in rules
        )
    lines.append(
        "- Day and night, meals, travel, sleep, and waiting advance the clock consistently."
    )
    return "\n".join(lines)


def parse_events(value: object) -> list[TimedEvent]:
    """Validate the host-supplied fixed-time story events."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("'events' must be a list of event objects")
    if len(value) > 50:
        raise ValueError("At most 50 scheduled events are allowed")
    events: list[TimedEvent] = []
    seen: set[str] = set()
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Each event must be an object with 'name'")
        name = clean_text(entry.get("name"), "event name", 80)
        folded = name.casefold()
        if folded in seen:
            raise ValueError("Event names must be unique.")
        seen.add(folded)
        if not isinstance(entry.get("public", False), bool):
            raise ValueError(f"Event '{name}': 'public' must be a boolean")
        events.append(
            TimedEvent(
                name=name,
                day=_bounded_int(entry.get("day"), "event day", 0, 100_000),
                minute=_bounded_int(entry.get("minute"), "event minute", 0, 1439),
                description=clean_text(entry.get("description"), "event description", 10_000),
                public=bool(entry.get("public", False)),
            )
        )
    return events


def parse_lorebook(value: object) -> list[LorebookEntry]:
    """Validate the host-supplied world-book entries."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("'lorebook' must be a list of world-book entry objects")
    if len(value) > 200:
        raise ValueError("At most 200 world-book entries are allowed")
    entries: list[LorebookEntry] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Each world-book entry must be an object with 'keys' and 'content'")
        keys = entry.get("keys")
        if not isinstance(keys, list) or not keys or len(keys) > 100:
            raise ValueError("Each world-book entry needs 1-100 keys")
        cleaned_keys = [clean_text(key, "world-book key", 40) for key in keys]
        if len({key.casefold() for key in cleaned_keys}) != len(cleaned_keys):
            raise ValueError("World-book keys must be unique within an entry.")
        title = clean_optional_text(entry.get("title"), "world-book title", 80)
        for flag in ("enabled", "constant"):
            if not isinstance(entry.get(flag, True), bool):
                raise ValueError(f"'{flag}' must be a boolean")
        entries.append(
            LorebookEntry(
                title=title,
                keys=cleaned_keys,
                content=clean_text(entry.get("content"), "world-book content", 10_000),
                order=_bounded_int(entry.get("order"), "world-book order", 0, 10_000),
                enabled=bool(entry.get("enabled", True)),
                constant=bool(entry.get("constant", False)),
            )
        )
    return entries


def parse_prompt_blocks(value: object) -> list[PromptBlock]:
    """Validate the host-supplied instruction blocks."""
    if value is None:
        return []
    if not isinstance(value, list):
        raise ValueError("'prompt_blocks' must be a list of instruction block objects")
    if len(value) > 100:
        raise ValueError("At most 100 prompt blocks are allowed")
    blocks: list[PromptBlock] = []
    for entry in value:
        if not isinstance(entry, dict):
            raise ValueError("Each prompt block must be an object with 'content'")
        position = entry.get("position", "output")
        if position not in ("system", "scenario", "output"):
            raise ValueError(f"Unknown prompt block position '{position}'.")
        if not isinstance(entry.get("enabled", True), bool):
            raise ValueError("'enabled' must be a boolean")
        blocks.append(
            PromptBlock(
                title=clean_optional_text(entry.get("title"), "prompt block title", 80),
                content=clean_text(entry.get("content"), "prompt block content", 10_000),
                position=position,
                enabled=bool(entry.get("enabled", True)),
            )
        )
    return blocks


def parse_sampling(value: object) -> SamplingConfig:
    """Validate optional per-room sampling overrides."""
    if value is None:
        return SamplingConfig()
    if not isinstance(value, dict):
        raise ValueError("'sampling' must be an object")

    def bounded_float(field: str, minimum: float, maximum: float) -> float | None:
        raw = value.get(field)
        if raw is None:
            return None
        if isinstance(raw, bool) or not isinstance(raw, (int, float)):
            raise ValueError(f"'sampling.{field}' must be a number")
        number = float(raw)
        if number < minimum or number > maximum:
            raise ValueError(f"'sampling.{field}' must be between {minimum} and {maximum}")
        return number

    return SamplingConfig(
        temperature=bounded_float("temperature", 0, 2),
        top_p=bounded_float("top_p", 0, 1),
    )


class LobbyMixin:
    """Lobby operations; authentication can atomically activate a transport connection."""

    async def process_payload(
        self: "GameEngine",
        client_id: str,
        payload: ClientPayload,
        *,
        authorize: Callable[[], bool] = lambda: True,
    ) -> None:
        """Route a validated client payload to its handler, restoring the turn on error."""
        token = CURRENT_OWNER.set(authorize)
        try:
            if not authorize():
                return
            handler = getattr(self, self.PAYLOAD_HANDLERS[payload.event_type])
            await handler(client_id, payload.data)
        except ValueError as exc:
            LOGGER.info("Client event rejected event=%s reason=%s", payload.event_type, exc)
            await self._send_error(client_id, str(exc))
            if payload.event_type == "action":
                async with self.effects_lock:
                    async with self.lock:
                        directive = (
                            self._next_turn_locked()
                            if authorize() and self.active_player_id == client_id
                            else None
                        )
                    if directive is not None:
                        await self.sender.send_personal(client_id, directive)
        finally:
            CURRENT_OWNER.reset(token)

    async def host_join(
        self: "GameEngine",
        client_id: str,
        data: dict[str, object],
        *,
        activate: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        """Create the room's host player on a fresh engine."""
        name = clean_text(data.get("name"), "name", 40)
        async with self.lock:
            if self.players:
                raise ValueError("The room already has players.")
            # Synchronous callback: socket promotion and domain authentication share the
            # same critical section. Invalid credentials never replace the old socket.
            if activate is not None:
                activate()
            player = Player(client_id, name, is_host=True, join_index=0)
            self.players[client_id] = player
            self.join_order.append(client_id)
            self.turn_queue.append(client_id)
            self.host_client_id = client_id
            self.state = GameState.SCENARIO_INJECTION
            snapshot = self._snapshot_locked(player)
        await self.sender.send_personal(client_id, ServerEvent(type="auth_ok", payload=snapshot))
        LOGGER.info("Host created room room=%s", self.room_code)
        await self.sender.broadcast_global(
            ServerEvent(type="system_msg", payload={"msg": f"{name} connected."})
        )
        return snapshot

    def _evict_player_locked(self: "GameEngine", client_id: str) -> None:
        """Remove a disconnected player's presence without touching name-owned claims."""
        if self.players.pop(client_id, None) is None:
            return
        if client_id in self.join_order:
            self.join_order.remove(client_id)
        self.turn_queue = deque(item for item in self.turn_queue if item != client_id)
        self.skip_votes.pop(client_id, None)
        # Never leave a dangling active turn pointing at an evicted player.
        if self.active_player_id == client_id:
            self.active_player_id = None

    def _canonical_character(self: "GameEngine", character_name: str) -> str:
        """Return the canonical cast name for a claimed character."""
        for c in self.cast:
            if c.name.casefold() == character_name.casefold():
                return c.name
        raise ValueError("That character does not exist in this room.")

    async def player_join(
        self: "GameEngine",
        client_id: str,
        data: dict[str, object],
        *,
        activate: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        """Join a room by claiming a character, or reconnect an existing player.

        An empty character joins as a spectator who watches and chats without
        acting. Character seats are owned by player name, so a returning player
        may re-claim their own character or switch to spectating. Mid-game
        claimers become players in the next round.
        """
        name = clean_text(data.get("name"), "name", 40)
        character_name = clean_optional_text(data.get("character"), "character", 40)
        observer = not character_name
        directive = None
        actions = None
        mid_game = False
        async with self.lock:
            existing = self.players.get(client_id)
            pending_existing = self.pending_players.get(client_id) if existing is None else None
            # A reconnect only applies when the name matches the stored player;
            # a different name means this device/session is now someone else.
            if existing is not None and existing.name.casefold() != name.casefold():
                existing = None
            if pending_existing is not None and pending_existing.name.casefold() != name.casefold():
                pending_existing = None
            target = existing if existing is not None else pending_existing
            rejoined = target is not None

            if rejoined:
                reconnect_token = data.get("reconnect_token")
                if not isinstance(reconnect_token, str) or not hmac.compare_digest(
                    reconnect_token, target.reconnect_token
                ):
                    # The client id plus name is the device's own identity, so a
                    # stale or missing token never blocks reconnecting in place.
                    # The token is advisory only: it is never a rejection reason.
                    LOGGER.info(
                        "Reconnect token stale room=%s name=%s; reconnecting by device identity",
                        self.room_code,
                        name,
                    )

            if target is not None:
                if not observer:
                    character_name = self._canonical_character(character_name)
                mid_game = self.state in {GameState.ACTIVE_TURN, GameState.AWAITING_LLM}
                previous_char = target.character_name
                if observer:
                    target.character_name = None
                else:
                    holder = self.claims.get(character_name)
                    if holder is not None and holder.casefold() != name.casefold():
                        raise ValueError("That character has already been claimed.")
                    if (
                        mid_game
                        and previous_char != character_name
                        and client_id in self.round_buffer
                    ):
                        raise ValueError("That seat is mid-round; try again shortly.")
                    target.character_name = character_name
                    self.claims[character_name] = name
                    if mid_game and previous_char != character_name:
                        target.handover_pending = True
                if previous_char and previous_char != target.character_name:
                    if self.claims.get(previous_char) == name:
                        self.claims.pop(previous_char, None)
            else:
                if self.state not in {
                    GameState.AWAITING_PLAYERS,
                    GameState.ACTIVE_TURN,
                    GameState.AWAITING_LLM,
                }:
                    raise ValueError("The game is not accepting new players.")
                if not observer:
                    character_name = self._canonical_character(character_name)
                mid_game = self.state in {GameState.ACTIVE_TURN, GameState.AWAITING_LLM}
                online = next(
                    (
                        p
                        for p in self.players.values()
                        if p.name.casefold() == name.casefold() and p.is_connected
                    ),
                    None,
                )
                if online is not None:
                    raise ValueError("That player name is already online.")
                for old in [
                    p
                    for p in self.players.values()
                    if p.name.casefold() == name.casefold() or p.client_id == client_id
                ]:
                    if old.client_id in self.round_buffer:
                        raise ValueError("Your seat is mid-round; try again shortly.")
                    self._evict_player_locked(old.client_id)
                if not observer:
                    holder = self.claims.get(character_name)
                    if holder is not None and holder.casefold() != name.casefold():
                        raise ValueError("That character has already been claimed.")
                    other_char = next(
                        (
                            p
                            for p in self.players.values()
                            if p.name.casefold() == name.casefold() and p.character_name is not None
                        ),
                        None,
                    )
                    if other_char is not None:
                        raise ValueError("That player name already has a character seat.")
            # Synchronous callback: socket promotion and domain authentication share the
            # same critical section. Invalid credentials never replace the old socket.
            if activate is not None:
                activate()
            if rejoined:
                if not target.is_connected:
                    target.connection_version += 1
                    if existing is not None and mid_game:
                        target.return_pending = True
                catchup_info: dict[str, object] | None = None
                if self.time_enabled and target.last_seen_total is not None:
                    now = self._clock_total()
                    missed = self._missed_events_since(target.last_seen_total)
                    catchup_info = {
                        "missed_minutes": max(0, now - target.last_seen_total),
                        "missed_events": [entry["name"] for entry in missed],
                        "from": GameClock.label_from_total(target.last_seen_total),
                        "to": self.game_clock.format(),
                    }
                    if missed:
                        target.catchup_notes.append(self._render_catchup_note(target, missed))
                    target.last_seen_total = now
                target.is_connected = True
                name = target.name
                player = target
            else:
                join_index = len(self.join_order) + len(self.pending_players)
                player = Player(
                    client_id,
                    name,
                    is_host=(client_id == self.host_client_id),
                    join_index=join_index,
                    character_name=character_name or None,
                )
                if self.time_enabled:
                    player.last_seen_total = self._clock_total()
                if observer:
                    # A spectator never acts, so it joins immediately in any state.
                    self.players[client_id] = player
                    self.join_order.append(client_id)
                    self.turn_queue.append(client_id)
                else:
                    self.claims[character_name] = name
                    player.handover_pending = mid_game
                    if not mid_game or (
                        self.state is GameState.ACTIVE_TURN and not self.round_buffer
                    ):
                        self.players[client_id] = player
                        self.join_order.append(client_id)
                        self.turn_queue.append(client_id)
                    else:
                        self.pending_players[client_id] = player
            active = self.players.get(self.active_player_id) if self.active_player_id else None
            if self.state is GameState.ACTIVE_TURN and (active is None or not active.is_connected):
                directive = self._next_turn_locked()
                actions = self._take_complete_round_locked()
                if actions is not None:
                    self._launch_round_locked(actions)
            snapshot = self._snapshot_locked(player)
            if rejoined and catchup_info is not None:
                snapshot["catchup"] = catchup_info
        await self.sender.send_personal(client_id, ServerEvent(type="auth_ok", payload=snapshot))
        LOGGER.info(
            "Player joined room=%s reconnect=%s character=%s",
            self.room_code,
            rejoined,
            player.character_name,
        )
        if rejoined:
            message = f"{name} rejoined."
        elif player.character_name:
            message = f"{name} connected as {player.character_name}."
        else:
            message = f"{name} joined as a spectator."
        await self.sender.broadcast_global(ServerEvent(type="system_msg", payload={"msg": message}))
        await self.sender.broadcast_except(client_id, self._player_roster_event())
        if directive is not None:
            await self.sender.broadcast_global(directive)
        return snapshot

    @staticmethod
    def _password_matches(digest: str, password: str | None, client_id: str) -> bool:
        """Return whether a digest matches the password bound to a client."""
        if not password:
            return False
        expected = hashlib.sha256(f"{password}{client_id}".encode()).hexdigest()
        return hmac.compare_digest(digest, expected)

    async def _chat(self: "GameEngine", client_id: str, data: dict[str, object]) -> None:
        """Broadcast a player's chat message to all connections."""
        message = clean_text(data.get("message"), "message", 10_000)
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            player = self.players.get(client_id) or self.pending_players.get(client_id)
            if player is None or not player.is_connected:
                raise ValueError("Authenticate before chatting.")
        await self.sender.broadcast_global(
            ServerEvent(type="chat_echo", payload={"name": player.name, "chat": message})
        )

    async def _update_character(
        self: "GameEngine", client_id: str, data: dict[str, object]
    ) -> None:
        """Let a player edit the card of the character they currently play."""
        description = clean_optional_text(data.get("description"), "description", 50_000)
        personality = clean_optional_text(data.get("personality"), "personality", 10_000)
        style = clean_optional_text(data.get("style"), "style", 10_000)
        example_dialogue = clean_optional_text(
            data.get("example_dialogue"), "example_dialogue", 10_000
        )
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            player = self.players.get(client_id)
            if player is None or not player.is_connected:
                raise ValueError("Authenticate before updating a character.")
            character_name = player.character_name
            if character_name is None:
                raise ValueError("You are not playing a character.")
            holder = self.claims.get(character_name)
            if holder is None or holder.casefold() != player.name.casefold():
                raise ValueError("You do not own this character's seat.")
            for char in self.cast:
                if char.name == character_name:
                    char.description = description
                    char.personality = personality
                    char.style = style
                    char.example_dialogue = example_dialogue
                    break
            set_cast = getattr(self.resolver, "set_cast", None)
            if set_cast is not None:
                set_cast(self.cast)
        LOGGER.info(
            "Character card updated room=%s character=%s",
            self.room_code,
            character_name,
        )
        await self.sender.broadcast_global(
            ServerEvent(type="system_msg", payload={"msg": f"{player.name} 更新了角色卡。"})
        )
        await self.sender.broadcast_global(self._player_roster_event())

    async def _initialize_scenario(
        self: "GameEngine", client_id: str, data: dict[str, object]
    ) -> None:
        """Accept the host's scenario, cast of characters and optional own claim."""
        scenario = clean_text(data.get("scenario"), "scenario", 500_000)
        guidance = clean_optional_text(data.get("guidance"), "guidance", 500_000)
        cast = parse_cast(data.get("characters"))
        time_config = parse_time_config(data.get("time_config"))
        time_rules = parse_time_rules(data.get("time_rules"))
        events = parse_events(data.get("events"))
        if events and not time_config.enabled:
            raise ValueError("Scheduled events require an enabled in-game clock.")
        lorebook = parse_lorebook(data.get("lorebook"))
        prompt_blocks = parse_prompt_blocks(data.get("prompt_blocks"))
        sampling = parse_sampling(data.get("sampling"))
        random_turn_order = data.get("random_turn_order", True)
        if not isinstance(random_turn_order, bool):
            raise ValueError("'random_turn_order' must be a boolean")
        host_character = clean_optional_text(data.get("host_character"), "host_character", 40)
        if host_character:
            claimed = next(
                (c for c in cast if c.name.casefold() == host_character.casefold()), None
            )
            if claimed is None:
                raise ValueError("The host's character does not exist in the cast.")
            host_character = claimed.name
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            player = self.players.get(client_id)
            if player is None or not player.is_host:
                raise ValueError("Only the host can initialize the scenario.")
            if self.state not in {GameState.SCENARIO_INJECTION, GameState.AWAITING_PLAYERS}:
                raise ValueError("The scenario cannot be changed in the current state.")
            previous_claims = dict(self.claims)
            self.cast = cast
            self.timed_events = events
            self.fired_events = set()
            self.event_log = []
            self.pending_event_injections = []
            self.lorebook = lorebook
            self.prompt_blocks = prompt_blocks
            self.sampling = sampling
            self.random_turn_order = random_turn_order
            new_names = {char.name for char in cast}
            # Editing before the start keeps claims that still exist and releases
            # the rest; the host's own claim is reassigned explicitly below.
            self.claims = {
                char_name: holder
                for char_name, holder in previous_claims.items()
                if char_name in new_names and holder != player.name
            }
            for member in self.players.values():
                if member.character_name not in new_names:
                    member.character_name = None
            if host_character:
                self.claims[host_character] = player.name
                player.character_name = host_character
            else:
                # A host without a claim observes the game and never acts.
                player.character_name = None
            failure_state = self.state
            self._launch_job_locked(
                lambda epoch: self._prepare_scenario(
                    epoch,
                    client_id,
                    scenario,
                    guidance,
                    cast,
                    time_config,
                    time_rules,
                    lorebook,
                    prompt_blocks,
                    sampling,
                ),
                failure_state,
            )

    async def _prepare_scenario(
        self: "GameEngine",
        epoch: int,
        client_id: str,
        scenario: str,
        guidance: str,
        cast: list[Character],
        time_config: TimeConfig,
        time_rules: list[TimeRule],
        lorebook: list[LorebookEntry],
        prompt_blocks: list[PromptBlock],
        sampling: SamplingConfig,
    ) -> None:
        """Generate only the title and open the lobby for players."""
        time_block = render_time_block(time_config, time_rules)
        self.resolver.set_genesis(scenario, guidance, time_context=time_block or None)
        set_cast = getattr(self.resolver, "set_cast", None)
        if set_cast is not None:
            set_cast(cast)
        set_lorebook = getattr(self.resolver, "set_lorebook", None)
        if set_lorebook is not None:
            set_lorebook(lorebook)
        set_prompt_blocks = getattr(self.resolver, "set_prompt_blocks", None)
        if set_prompt_blocks is not None:
            set_prompt_blocks(prompt_blocks)
        set_sampling = getattr(self.resolver, "set_sampling", None)
        if set_sampling is not None:
            set_sampling(sampling)
        LOGGER.info(
            "Scenario config applied blocks=%d sampling=%s",
            sum(1 for block in prompt_blocks if block.enabled),
            sampling.model_dump(),
        )
        title = await self.resolver.generate_scenario_title()
        async with self.effects_lock:
            async with self.lock:
                if not self._job_current(epoch):
                    return
                self.original_scenario = scenario
                self.private_guidance = guidance
                self.scenario_title = title
                self.time_enabled = time_config.enabled
                self.game_clock = GameClock(
                    day=time_config.start_day, minute=time_config.start_minute
                )
                self.max_elapsed_minutes = time_config.max_elapsed_minutes
                self.default_elapsed_minutes = time_config.default_elapsed_minutes
                self.time_rules = time_rules
                self.state = GameState.AWAITING_PLAYERS
            await self.sender.send_personal(
                client_id,
                ServerEvent(
                    type="scenario_ready",
                    payload={
                        "title": self.scenario_title,
                        "characters": self._characters_payload(),
                    },
                ),
            )
            await self.sender.broadcast_except(client_id, self._player_roster_event())
            await self._publish_usage(client_id)
            LOGGER.info("Scenario title ready; lobby accepting players")

    async def _start_game(self: "GameEngine", client_id: str, data: dict[str, object]) -> None:
        """Accept the host's start command and launch the start job."""
        del data
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            player = self.players.get(client_id)
            if player is None or not player.is_host:
                raise ValueError("Only the host can start the game.")
            if self.state is not GameState.AWAITING_PLAYERS:
                raise ValueError("The game cannot be started in the current state.")
            cast = list(self.cast)
            claims = dict(self.claims)
            self._launch_job_locked(
                lambda epoch: self._prepare_start(epoch, cast, claims),
                GameState.AWAITING_PLAYERS,
            )

    async def _prepare_start(
        self: "GameEngine", epoch: int, cast: list[Character], claims: dict[str, str]
    ) -> None:
        """Introduce the cast and transition the game to its active turn."""
        resolution = await self.resolver.generate_start_state(
            cast, claims, current_time=self._authoritative_time_label()
        )
        async with self.effects_lock:
            async with self.lock:
                if not self._job_current(epoch):
                    return
            await self.transcript.start(
                self.scenario_title or "Untitled Session",
                resolution.global_narrative,
                opening_scenario=self.original_scenario,
                private_guidance=self.private_guidance,
            )
            async with self.lock:
                if not self._job_current(epoch):
                    return
                self.current_scenario_state = resolution.global_narrative
                self.opening_scenario = resolution.global_narrative
                self.state = GameState.ACTIVE_TURN
                self._shuffle_turn_queue_locked()
                directive = self._next_turn_locked()
            payload = resolution.model_dump()
            payload.update(
                round_title=self.scenario_title,
                game_time=self._authoritative_time_label(),
                time_elapsed_minutes=None,
            )
            await self.sender.broadcast_global(ServerEvent(type="state_update", payload=payload))
            await self.sender.broadcast_global(
                ServerEvent(type="system_msg", payload={"msg": "The game has started."})
            )
            await self.sender.broadcast_global(
                ServerEvent(type="round_start", payload={"round_number": 1})
            )
            if directive is not None:
                await self.sender.broadcast_global(directive)
            LOGGER.info("Game started characters=%d claimed=%d", len(cast), len(claims))

    async def _end_game(self: "GameEngine", client_id: str, data: dict[str, object]) -> None:
        """Validate the host's end command and shut down the session."""
        del data
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            player = self.players.get(client_id)
            if player is None or not player.is_host:
                raise ValueError("Only the host can end the game.")
            if self.state not in {GameState.ACTIVE_TURN, GameState.AWAITING_LLM}:
                raise ValueError("The game cannot be ended in the current state.")
        await self.shutdown()

    async def _close_room(self: "GameEngine", client_id: str, data: dict[str, object]) -> None:
        """Let the host dissolve the room from any state."""
        del data
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            player = self.players.get(client_id) or self.pending_players.get(client_id)
            if player is None or not player.is_host:
                raise ValueError("Only the host can close the room.")
        await self.shutdown(reason="The host closed the room.")

    def _characters_payload(self: "GameEngine") -> list[dict[str, object]]:
        """Build the public cast payload with claim status."""
        characters = []
        for char in self.cast:
            holder_name = self.claims.get(char.name)
            connected = False
            if holder_name is not None:
                connected = any(
                    p.name.casefold() == holder_name.casefold()
                    and p.character_name == char.name
                    and p.is_connected
                    for p in self.players.values()
                )
            characters.append(
                {
                    "name": char.name,
                    "description": char.description,
                    "personality": char.personality,
                    "style": char.style,
                    "example_dialogue": char.example_dialogue,
                    "claimed_by": holder_name,
                    "connected": connected,
                }
            )
        return characters

    def room_info_payload(self: "GameEngine") -> dict[str, object]:
        """Build the public room-info payload for pre-join character picking."""
        return {
            "invite_code": self.room_code,
            "state": self.state.name,
            "scenario_title": self.scenario_title,
            "accepting_new": self.state
            in {
                GameState.AWAITING_PLAYERS,
                GameState.ACTIVE_TURN,
                GameState.AWAITING_LLM,
            },
            "characters": self._characters_payload(),
        }

    def _player_roster_event(self: "GameEngine") -> ServerEvent:
        """Build a player roster event from the current players and cast."""
        players = [
            {
                "name": p.name,
                "character": p.character_name,
                "connected": p.is_connected,
                "is_host": p.is_host,
            }
            for p in [
                *(self.players[player_id] for player_id in self.join_order),
                *self.pending_players.values(),
            ]
        ]
        return ServerEvent(
            type="player_roster",
            payload={"players": players, "characters": self._characters_payload()},
        )

    def _snapshot_locked(self: "GameEngine", player: Player) -> dict[str, object]:
        """Build the reconnect snapshot for an authenticated player."""
        active_player = (
            self.players.get(self.active_player_id) if self.active_player_id is not None else None
        )
        return {
            "client_id": player.client_id,
            "reconnect_token": player.reconnect_token,
            "invite_code": self.room_code,
            "round_paused": self.round_paused,
            "name": player.name,
            "character": player.character_name,
            "is_host": player.is_host,
            "state": self.state.name,
            "game_time": self._authoritative_time_label(),
            "round_history": list(self.round_history),
            "scenario_title": self.scenario_title,
            "opening_scenario": self.opening_scenario,
            "scenario_state": self.current_scenario_state,
            "completed_round_number": self.round_counter or None,
            "active_player_id": self.active_player_id,
            "active_player_name": (
                (active_player.character_name or active_player.name)
                if active_player is not None
                else None
            ),
            "round_number": (
                self.round_counter + 1
                if self.state in {GameState.ACTIVE_TURN, GameState.AWAITING_LLM}
                else None
            ),
            "submitted_actions": self._submitted_actions_locked(),
            "player_order": [
                (self.players[item].character_name or self.players[item].name)
                for item in self.join_order
            ],
            "players": [
                {
                    "name": p.name,
                    "character": p.character_name,
                    "connected": p.is_connected,
                    "is_host": p.is_host,
                }
                for p in [
                    *(self.players[player_id] for player_id in self.join_order),
                    *self.pending_players.values(),
                ]
            ],
            "characters": self._characters_payload(),
        }

"""Authentication, scenario creation, chat, and lobby transitions."""

import hashlib
import hmac
import logging
from collections.abc import Callable
from contextvars import ContextVar
from typing import TYPE_CHECKING

from core.config import settings
from core.schemas import Character, ClientPayload, ServerEvent
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
        folded = char_name.casefold()
        if folded in seen:
            raise ValueError("Character names must be unique.")
        seen.add(folded)
        cast.append(Character(name=char_name, description=description))
    return cast


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

    async def player_join(
        self: "GameEngine",
        client_id: str,
        data: dict[str, object],
        *,
        activate: Callable[[], None] | None = None,
    ) -> dict[str, object]:
        """Join a room by claiming a character, or reconnect an existing player.

        Mid-game joiners become players in the next round: they activate
        immediately only when the current round has no actions collected yet.
        """
        name = clean_text(data.get("name"), "name", 40)
        character_name = clean_text(data.get("character"), "character", 40)
        directive = None
        actions = None
        mid_game = False
        async with self.lock:
            existing = self.players.get(client_id)
            pending_existing = self.pending_players.get(client_id) if existing is None else None
            target = existing if existing is not None else pending_existing
            rejoined = target is not None
            if target is not None:
                reconnect_token = data.get("reconnect_token")
                if not isinstance(reconnect_token, str) or not hmac.compare_digest(
                    reconnect_token, target.reconnect_token
                ):
                    raise ValueError("Invalid reconnect token for this session.")
                if (
                    target.character_name is None
                    or target.character_name.casefold() != character_name.casefold()
                ):
                    raise ValueError("Invalid character for this session.")
            else:
                if self.state not in {
                    GameState.AWAITING_PLAYERS,
                    GameState.ACTIVE_TURN,
                    GameState.AWAITING_LLM,
                }:
                    raise ValueError("The game is not accepting new players.")
                if any(p.name.casefold() == name.casefold() for p in self.players.values()) or any(
                    p.name.casefold() == name.casefold() for p in self.pending_players.values()
                ):
                    raise ValueError("That player name is already in use.")
                character = next(
                    (c for c in self.cast if c.name.casefold() == character_name.casefold()),
                    None,
                )
                if character is None:
                    raise ValueError("That character does not exist in this room.")
                character_name = character.name
                if character_name in self.claims:
                    raise ValueError("That character has already been claimed.")
                mid_game = self.state in {GameState.ACTIVE_TURN, GameState.AWAITING_LLM}
            # Synchronous callback: socket promotion and domain authentication share the
            # same critical section. Invalid credentials never replace the old socket.
            if activate is not None:
                activate()
            if rejoined:
                if not target.is_connected:
                    target.connection_version += 1
                    if existing is not None and self.state in {
                        GameState.ACTIVE_TURN,
                        GameState.AWAITING_LLM,
                    }:
                        target.return_pending = True
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
                    character_name=character_name,
                )
                self.claims[character_name] = client_id
                player.handover_pending = mid_game
                if not mid_game or (self.state is GameState.ACTIVE_TURN and not self.round_buffer):
                    self.players[client_id] = player
                    self.join_order.append(client_id)
                    self.turn_queue.append(client_id)
                else:
                    self.pending_players[client_id] = player
            if self.state is GameState.ACTIVE_TURN and (
                self.active_player_id is None
                or not self.players[self.active_player_id].is_connected
            ):
                directive = self._next_turn_locked()
                actions = self._take_complete_round_locked()
                if actions is not None:
                    self._launch_round_locked(actions)
            snapshot = self._snapshot_locked(player)
        await self.sender.send_personal(client_id, ServerEvent(type="auth_ok", payload=snapshot))
        LOGGER.info(
            "Player joined room=%s reconnect=%s character=%s",
            self.room_code,
            rejoined,
            player.character_name,
        )
        if rejoined:
            message = f"{name} rejoined."
        else:
            message = f"{name} connected as {player.character_name}."
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

    async def _initialize_scenario(
        self: "GameEngine", client_id: str, data: dict[str, object]
    ) -> None:
        """Accept the host's scenario, cast of characters and own claim."""
        scenario = clean_text(data.get("scenario"), "scenario", 500_000)
        guidance = clean_optional_text(data.get("guidance"), "guidance", 500_000)
        cast = parse_cast(data.get("characters"))
        host_character = clean_text(data.get("host_character"), "host_character", 40)
        claimed = next((c for c in cast if c.name.casefold() == host_character.casefold()), None)
        if claimed is None:
            raise ValueError("The host must claim one of the room's characters.")
        host_character = claimed.name
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            player = self.players.get(client_id)
            if player is None or not player.is_host:
                raise ValueError("Only the host can initialize the scenario.")
            if self.state is not GameState.SCENARIO_INJECTION:
                raise ValueError("The scenario cannot be changed in the current state.")
            self.cast = cast
            self.claims = {host_character: client_id}
            player.character_name = host_character
            self._launch_job_locked(
                lambda epoch: self._prepare_scenario(epoch, client_id, scenario, guidance),
                GameState.SCENARIO_INJECTION,
            )

    async def _prepare_scenario(
        self: "GameEngine", epoch: int, client_id: str, scenario: str, guidance: str
    ) -> None:
        """Generate only the title and open the lobby for players."""
        self.resolver.set_genesis(scenario, guidance)
        title = await self.resolver.generate_scenario_title()
        async with self.effects_lock:
            async with self.lock:
                if not self._job_current(epoch):
                    return
                self.original_scenario = scenario
                self.private_guidance = guidance
                self.scenario_title = title
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
            claims = {
                char: self.players[cid].name
                for char, cid in self.claims.items()
                if cid in self.players
            }
            self._launch_job_locked(
                lambda epoch: self._prepare_start(epoch, cast, claims),
                GameState.AWAITING_PLAYERS,
            )

    async def _prepare_start(
        self: "GameEngine", epoch: int, cast: list[Character], claims: dict[str, str]
    ) -> None:
        """Introduce the cast and transition the game to its active turn."""
        resolution = await self.resolver.generate_start_state(cast, claims)
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
                directive = self._next_turn_locked()
            payload = resolution.model_dump()
            payload.update(round_title=self.scenario_title)
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
            claimant_id = self.claims.get(char.name)
            claimed_by = None
            connected = False
            if claimant_id is not None:
                holder = self.players.get(claimant_id) or self.pending_players.get(claimant_id)
                if holder is not None:
                    claimed_by = holder.name
                    connected = holder.is_connected
            characters.append(
                {
                    "name": char.name,
                    "description": char.description,
                    "claimed_by": claimed_by,
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

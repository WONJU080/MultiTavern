"""Atomic game state with owned, cancellable inference tasks."""

import asyncio
import json
import logging
import random
from collections import deque
from collections.abc import Awaitable, Callable
from pathlib import Path

from core.config import settings
from core.schemas import (
    Character,
    LorebookEntry,
    PromptBlock,
    SamplingConfig,
    ServerEvent,
    TimedEvent,
    TimeRule,
)
from logic.dice import roll_d100
from logic.game_clock import GameClock
from logic.llm_manager import LLMBackendUnavailableError, LLMResolutionError
from logic.lobby import CURRENT_OWNER, LobbyMixin
from logic.models import EventSender, GameState, Player, ResolutionManager
from logic.presentation import name_resolution
from logic.round_history import RoundHistoryStore
from logic.transcript import GameTranscript
from logic.validation import clean_text

LOGGER = logging.getLogger(__name__)
IDLE_ACTION = "[SYSTEM INJECTION: Player disconnected. Idle.]"
REPEATED_ACTION_NOTE = (
    "[SYSTEM: This player repeats their exact previous action. Resolve it definitively "
    "this round: state clearly whether it succeeds or fails and how the situation changes. "
    "Use a d100 check when the outcome is genuinely uncertain. Do not leave the attempt "
    "unresolved or restate the same failure without progress.]"
)
HANDOVER_NOTE = (
    "[SYSTEM: This character was just claimed by a player and is no longer DM-controlled. "
    "Describe the in-world transition into player control, then follow the player's action "
    "from now on.]"
)
SKIP_ACTION = (
    "[SYSTEM: This character's player input was skipped by a unanimous vote of the other "
    "players. Decide a plausible in-world action for this character, consistent with their "
    "established personality and goals, and resolve it like any other action.]"
)
SKIP_DISPLAY = "(skipped by vote — AI decided)"
MAX_ROUND_HISTORY = 100


class GameEngine(LobbyMixin):
    """One session; state lock is never held over network or filesystem work."""

    PAYLOAD_HANDLERS = {
        "chat": "_chat",
        "scenario_init": "_initialize_scenario",
        "start_game": "_start_game",
        "end_game": "_end_game",
        "close_room": "_close_room",
        "action": "_submit_action",
        "skip_vote": "_skip_vote",
        "retry_round": "_retry_round",
        "history_request": "_request_history",
        "character_update": "_update_character",
    }

    def __init__(self, sender: EventSender, resolver: ResolutionManager) -> None:
        """Initialize the engine with its event sender and resolution backend."""
        self.sender, self.resolver = sender, resolver
        self.state = GameState.AWAITING_HOST
        self.room_code: str | None = None
        self.on_ended: Callable[[str], Awaitable[None]] | None = None
        self.players: dict[str, Player] = {}
        self.join_order: list[str] = []
        self.turn_queue: deque[str] = deque()
        self.round_buffer: dict[str, str] = {}
        self.previous_actions: dict[str, str] = {}
        self.cast: list[Character] = []
        self.claims: dict[str, str] = {}
        self.pending_players: dict[str, Player] = {}
        self.skip_votes: dict[str, set[str]] = {}
        self.host_client_id: str | None = None
        self._disconnect_tasks: dict[str, asyncio.Task] = {}
        self.active_player_id: str | None = None
        self.lock = asyncio.Lock()
        # Order committed transcript/events against end-game, without holding the
        # state lock or serializing chat behind model inference.
        self.effects_lock = asyncio.Lock()
        self.generation = 0
        self.inference_task: asyncio.Task | None = None
        self.round_paused = False
        self.pending_resolution: dict | None = None
        self.round_counter = 0
        self.scenario_title: str | None = None
        self.original_scenario: str | None = None
        self.private_guidance = ""
        self.current_scenario_state: str | None = None
        self.opening_scenario: str | None = None
        self.time_enabled = False
        self.game_clock = GameClock()
        self.max_elapsed_minutes = 600
        self.default_elapsed_minutes = 15
        self.time_rules: list[TimeRule] = []
        self.timed_events: list[TimedEvent] = []
        self.fired_events: set[str] = set()
        self.event_log: list[dict[str, object]] = []
        self.pending_event_injections: list[str] = []
        self.lorebook: list[LorebookEntry] = []
        self.prompt_blocks: list[PromptBlock] = []
        self.sampling: SamplingConfig = SamplingConfig()
        self.round_history: list[dict[str, object]] = []
        self.history_store = RoundHistoryStore()
        self.random_turn_order = True
        self.transcript = GameTranscript()

    def _authoritative_time_label(self) -> str:
        """Return the server-owned clock label when time tracking is enabled."""
        return self.game_clock.format() if self.time_enabled else ""

    def _shuffle_turn_queue_locked(self) -> None:
        """Randomize the per-round action input order when enabled."""
        if not self.random_turn_order:
            return
        items = list(self.turn_queue)
        random.shuffle(items)
        self.turn_queue = deque(items)

    def _clock_total(self) -> int:
        """Return the absolute in-game minute count."""
        return self.game_clock.day * GameClock.MINUTES_PER_DAY + self.game_clock.minute

    def _missed_events_since(self, since_total: int | None) -> list[dict[str, object]]:
        """Return logged global events that fired after the given absolute minute."""
        if since_total is None:
            return []
        return [
            entry for entry in self.event_log if int(entry["fired_total_minutes"]) > since_total
        ]

    def _render_catchup_note(self, player: "Player", missed: list[dict[str, object]]) -> str:
        """Render the backstage note asking the DM to summarize a returner's absence."""
        events = "; ".join(f"{entry['name']}: {entry['description']}" for entry in missed)
        return (
            f"[SYSTEM: {player.name} was offline while the in-game clock advanced to "
            f"{self.game_clock.format()}. During their absence these global events occurred: "
            f"{events}. Briefly summarize what this character witnessed or learned of these "
            "events in their outcome; they are now aligned to the global time.]"
        )

    def _job_current(self, epoch: int) -> bool:
        """Return whether the job's epoch is current and the session has not ended."""
        return self.generation == epoch and self.state is not GameState.ENDED

    def _launch_job_locked(
        self, work: Callable[[int], Awaitable[None]], failure_state: GameState
    ) -> None:
        """Start an owned inference task for the given work and failure state."""
        self.generation += 1
        self.state = GameState.AWAITING_LLM
        self.round_paused = False
        self.inference_task = asyncio.create_task(
            self._run_job(work, self.generation, failure_state)
        )

    async def _run_job(
        self, work: Callable[[int], Awaitable[None]], epoch: int, failure_state: GameState
    ) -> None:
        """Run an owned inference job, handling failures and cleanup."""
        LOGGER.info("Inference job started generation=%d phase=%s", epoch, failure_state.name)
        try:
            async with self.effects_lock:
                if not self._job_current(epoch):
                    return
                await self.sender.broadcast_global(
                    ServerEvent(type="dm_thinking", payload={"active": True})
                )
            async with asyncio.timeout(settings.llm.request_timeout_seconds * 3):
                await work(epoch)
            LOGGER.info(
                "Inference job finished generation=%d current=%s state=%s",
                epoch,
                self._job_current(epoch),
                self.state.name,
            )
        except asyncio.CancelledError:
            LOGGER.info("Inference job cancelled generation=%d", epoch)
            raise
        except Exception as exc:
            # Boundary for an owned task: consume failures so the session never
            # remains spinning forever. Do not expose provider bodies/private guidance.
            LOGGER.warning("Inference job failed generation=%d error=%s", epoch, type(exc).__name__)
            connection_hint = (
                "Could not connect to the LLM backend. Check that the model server is running "
                "and its configured endpoint is reachable before retrying. "
                if isinstance(exc, LLMBackendUnavailableError)
                else ""
            )
            async with self.effects_lock:
                async with self.lock:
                    if not self._job_current(epoch):
                        return
                    self.state = failure_state
                    self.round_paused = failure_state is GameState.AWAITING_LLM
                await self.sender.broadcast_global(
                    ServerEvent(
                        type="error",
                        payload={
                            "msg": (
                                connection_hint
                                + (
                                    "Round paused; actions and dice are retained. "
                                    "The host can retry or end."
                                    if self.round_paused
                                    else "Could not prepare the game. Please try again."
                                )
                            ),
                            "round_paused": self.round_paused,
                            "state": self.state.name,
                        },
                    )
                )
        finally:
            async with self.effects_lock:
                if self._job_current(epoch):
                    await self._publish_usage()
                    await self.sender.broadcast_global(
                        ServerEvent(type="dm_thinking", payload={"active": False})
                    )
            if self.inference_task is asyncio.current_task():
                self.inference_task = None

    async def wait_for_inference(self) -> None:
        """Join owned work (used by shutdown callers and deterministic tests)."""
        task = self.inference_task
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)

    async def suspend(self) -> None:
        """Cancel in-flight inference and release the resolver, without ending.

        Used before a server restart so a persisted room can be resumed later
        without finalizing its transcript or broadcasting an end event.
        """
        async with self.lock:
            task = self.inference_task
            if task is not None:
                task.cancel()
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        for disconnect_task in self._disconnect_tasks.values():
            disconnect_task.cancel()
        self._disconnect_tasks.clear()
        close = getattr(self.resolver, "close", None)
        if close is not None:
            await close()

    def to_persistent_dict(self) -> dict[str, object]:
        """Serialize the room's game state for persistence across restarts."""
        serialize_resolver = getattr(self.resolver, "to_persistent_dict", None)
        return {
            "state": self.state.name,
            "cast": [char.model_dump() for char in self.cast],
            "claims": dict(self.claims),
            "players": [
                {
                    "client_id": player.client_id,
                    "name": player.name,
                    "is_host": player.is_host,
                    "join_index": player.join_index,
                    "character_name": player.character_name,
                    "reconnect_token": player.reconnect_token,
                    "connection_version": player.connection_version,
                    "last_seen_total": player.last_seen_total,
                }
                for player in self.players.values()
            ],
            "join_order": list(self.join_order),
            "host_client_id": self.host_client_id,
            "scenario_title": self.scenario_title,
            "original_scenario": self.original_scenario,
            "private_guidance": self.private_guidance,
            "current_scenario_state": self.current_scenario_state,
            "opening_scenario": self.opening_scenario,
            "round_counter": self.round_counter,
            "previous_actions": dict(self.previous_actions),
            "random_turn_order": self.random_turn_order,
            "time_enabled": self.time_enabled,
            "game_clock": self.game_clock.to_dict(),
            "max_elapsed_minutes": self.max_elapsed_minutes,
            "default_elapsed_minutes": self.default_elapsed_minutes,
            "time_rules": [rule.model_dump() for rule in self.time_rules],
            "timed_events": [event.model_dump() for event in self.timed_events],
            "fired_events": sorted(self.fired_events),
            "event_log": list(self.event_log),
            "lorebook": [entry.model_dump() for entry in self.lorebook],
            "prompt_blocks": [block.model_dump() for block in self.prompt_blocks],
            "sampling": self.sampling.model_dump(),
            "round_history": list(self.round_history),
            "transcript_path": str(self.transcript.path) if self.transcript.path else None,
            "resolver": serialize_resolver() if serialize_resolver is not None else {},
        }

    async def shutdown(
        self,
        *,
        close_resolver: bool = True,
        reason: str = "The host ended the game.",
        notify: bool = True,
    ) -> None:
        """Terminate the session, cancel inference and finalize the transcript."""
        LOGGER.info("Game shutdown requested close_resolver=%s reason=%s", close_resolver, reason)
        async with self.effects_lock:
            async with self.lock:
                already_ended = self.state is GameState.ENDED
                self.state = GameState.ENDED
                self.generation += 1
                self.active_player_id = None
                self.round_paused = False
                task = self.inference_task
                if task is not None:
                    task.cancel()
            try:
                await self.transcript.finalize(reason)
            except OSError:
                LOGGER.warning("Could not finalize game transcript")
            if not already_ended and notify:
                await self.sender.broadcast_global(
                    ServerEvent(type="game_ended", payload={"msg": reason})
                )
            if self.on_ended is not None:
                await self.on_ended(reason)
        if task is not None:
            await asyncio.gather(task, return_exceptions=True)
        for disconnect_task in self._disconnect_tasks.values():
            disconnect_task.cancel()
        self._disconnect_tasks.clear()
        if close_resolver:
            close = getattr(self.resolver, "close", None)
            if close is not None:
                await close()

    def connection_version_of(self, client_id: str) -> int | None:
        """Return the connection version of a player or pending joiner."""
        player = self.players.get(client_id) or self.pending_players.get(client_id)
        return player.connection_version if player is not None else None

    def schedule_disconnect(
        self,
        client_id: str,
        *,
        expected_version: int | None,
        still_disconnected: Callable[[], bool],
        delay: float,
    ) -> None:
        """Delay departure handling so brief dropouts do not count as leaving."""
        task = asyncio.create_task(
            self._delayed_disconnect(client_id, expected_version, still_disconnected, delay)
        )
        self._disconnect_tasks[client_id] = task

    async def _delayed_disconnect(
        self,
        client_id: str,
        expected_version: int | None,
        still_disconnected: Callable[[], bool],
        delay: float,
    ) -> None:
        if delay and delay > 0:
            await asyncio.sleep(delay)
        self._disconnect_tasks.pop(client_id, None)
        await self.handle_disconnect(
            client_id,
            expected_version=expected_version,
            still_disconnected=still_disconnected,
        )

    async def handle_disconnect(
        self,
        client_id: str,
        *,
        expected_version: int | None = None,
        still_disconnected: Callable[[], bool] = lambda: True,
    ) -> None:
        """Mark a player disconnected and advance or idle the active turn."""
        pending_removed = False
        async with self.effects_lock:
            async with self.lock:
                pending = self.pending_players.get(client_id)
                if pending is not None:
                    # A joiner who leaves before activation never entered the game;
                    # release the character back to the DM.
                    if not still_disconnected() or (
                        expected_version is not None
                        and pending.connection_version != expected_version
                    ):
                        return
                    self.pending_players.pop(client_id, None)
                    if pending.character_name:
                        self.claims.pop(pending.character_name, None)
                    pending_removed = True
                    LOGGER.info(
                        "Pending joiner left before activation room=%s player=%s",
                        self.room_code,
                        client_id,
                    )
                    return
                player = self.players.get(client_id)
                if player is None or not player.is_connected:
                    return
                if not still_disconnected() or (
                    expected_version is not None and player.connection_version != expected_version
                ):
                    return
                player.is_connected = False
                player.connection_version += 1
                player.departure_pending = True
                player.return_pending = False
                if self.time_enabled:
                    player.last_seen_total = (
                        self.game_clock.day * GameClock.MINUTES_PER_DAY + self.game_clock.minute
                    )
                directive = None
                if self.state is GameState.ACTIVE_TURN:
                    if self.active_player_id == client_id:
                        if any(p.is_connected for p in self.players.values()):
                            self.round_buffer[client_id] = IDLE_ACTION
                            self.turn_queue.rotate(-1)
                        else:
                            self.active_player_id = None
                    directive = self._next_turn_locked()
                    actions = self._take_complete_round_locked()
                    if actions is not None:
                        self._launch_round_locked(actions)
        if pending_removed:
            await self.sender.broadcast_global(self._player_roster_event())
            return
        await self.sender.broadcast_global(
            ServerEvent(type="system_msg", payload={"msg": f"{player.name} disconnected."})
        )
        await self.sender.broadcast_global(self._player_roster_event())
        if directive is not None:
            await self.sender.broadcast_global(directive)

    def _action_allowed(self, client_id: str) -> None:
        """Raise if the client cannot submit an action in the current state."""
        player = self.players.get(client_id)
        if player is None or not player.is_connected:
            raise ValueError("Authenticate before submitting an action.")
        if self.state is not GameState.ACTIVE_TURN:
            raise ValueError("Actions are blocked while no turn is active.")
        if client_id != self.active_player_id:
            raise ValueError("It is not your turn.")

    async def _submit_action(self, client_id: str, data: dict[str, object]) -> None:
        """Validate and buffer a player action, launching a round when complete."""
        action = clean_text(data.get("action"), "action", 50_000)
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            self._action_allowed(client_id)
            epoch = self.generation
            candidate = dict(self.round_buffer)
            candidate[client_id] = action
            named = {
                self.players[item].character_name
                or self.players[item].name: candidate.get(item, IDLE_ACTION)
                for item in self.join_order
                if self.players[item].character_name is not None
            }
            current_state = self.current_scenario_state or ""
        preflight = getattr(self.resolver, "preflight_round", None)
        if preflight is not None:
            try:
                await preflight(named, current_state)
            except LLMResolutionError as exc:
                raise ValueError(str(exc)) from exc
        async with self.effects_lock:
            async with self.lock:
                if not CURRENT_OWNER.get()() or epoch != self.generation:
                    return
                self._action_allowed(client_id)
                player = self.players[client_id]
                self.round_buffer[client_id] = action
                action_event = ServerEvent(
                    type="action_echo",
                    payload={
                        "round_number": self.round_counter + 1,
                        "player_name": player.character_name or player.name,
                        "player_color_index": player.join_index,
                        "action": action,
                    },
                )
                self.turn_queue.rotate(-1)
                directive = self._next_turn_locked()
                actions = self._take_complete_round_locked()
                if actions is not None:
                    self._launch_round_locked(actions)
            LOGGER.info("Player action accepted round=%d", action_event.payload["round_number"])
            await self.sender.broadcast_global(action_event)
            if directive is not None:
                await self.sender.broadcast_global(directive)

    async def _skip_vote(self, client_id: str, data: dict[str, object]) -> None:
        """Record a skip vote; a unanimous vote skips the active player's input."""
        del data
        vote_event = None
        directive = None
        skipped = False
        removed_target = ""
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            voter = self.players.get(client_id)
            if voter is None or not voter.is_connected:
                raise ValueError("Authenticate before voting.")
            if self.state is not GameState.ACTIVE_TURN:
                raise ValueError("Votes are only allowed during an active turn.")
            target = self.active_player_id
            if target is None:
                raise ValueError("There is no active turn to skip.")
            if target == client_id:
                raise ValueError("You cannot vote to skip yourself.")
            target_player = self.players[target]
            eligible = {
                player_id
                for player_id, player in self.players.items()
                if player_id != target and player.is_connected and player.character_name is not None
            }
            if not eligible:
                raise ValueError("No other players are online to vote.")
            votes = self.skip_votes.setdefault(target, set())
            votes.add(client_id)
            vote_event = ServerEvent(
                type="skip_vote",
                payload={
                    "target": target_player.character_name or target_player.name,
                    "voter": voter.name,
                    "votes": len(votes),
                    "needed": len(eligible),
                },
            )
            if eligible <= votes:
                skipped = True
                removed_target = target_player.character_name or target_player.name
                self.round_buffer[target] = SKIP_ACTION
                target_player.skip_pending = True
                self.turn_queue.rotate(-1)
                directive = self._next_turn_locked()
                actions = self._take_complete_round_locked()
                if actions is not None:
                    self._launch_round_locked(actions)
            LOGGER.info(
                "Skip vote room=%s voter=%s target=%s votes=%d/%d passed=%s",
                self.room_code,
                voter.name,
                removed_target or target,
                len(votes),
                len(eligible),
                skipped,
            )
        if vote_event is not None:
            await self.sender.broadcast_global(vote_event)
        if skipped:
            await self.sender.broadcast_global(
                ServerEvent(
                    type="system_msg",
                    payload={
                        "msg": (
                            f"Vote passed: {removed_target}'s input was skipped and "
                            "their player was removed from the game. They may rejoin."
                        )
                    },
                )
            )
        if directive is not None:
            await self.sender.broadcast_global(directive)

    def _next_turn_locked(self) -> ServerEvent | None:
        """Advance the turn queue and return a directive for the next active player."""
        if self.state is not GameState.ACTIVE_TURN:
            return None
        if not any(player.is_connected for player in self.players.values()):
            self.active_player_id = None
            return None
        for _ in range(len(self.turn_queue)):
            client_id = self.turn_queue[0]
            player = self.players.get(client_id)
            if player is None:
                # Stale queue entries cannot advance a turn.
                self.turn_queue.popleft()
                continue
            if player.character_name is None:
                # A host without a character observes and never acts.
                self.turn_queue.rotate(-1)
                continue
            if client_id in self.round_buffer:
                self.turn_queue.rotate(-1)
                continue
            if not player.is_connected:
                self.round_buffer[client_id] = IDLE_ACTION
                self.turn_queue.rotate(-1)
                continue
            self.active_player_id = client_id
            self.skip_votes.clear()
            return ServerEvent(
                type="turn_directive",
                payload={
                    "active_player_id": client_id,
                    "active_player_name": player.character_name or player.name,
                    "round_number": self.round_counter + 1,
                    "submitted_actions": self._submitted_actions_locked(),
                    "player_order": [
                        (self.players[player_id].character_name or self.players[player_id].name)
                        for player_id in self.join_order
                    ],
                },
            )
        self.active_player_id = None
        self.skip_votes.clear()
        return None

    def _submitted_actions_locked(self) -> dict[str, str]:
        """Return non-idle, non-skipped actions keyed by character name."""
        return {
            self.players[client_id].character_name or self.players[client_id].name: action
            for client_id, action in self.round_buffer.items()
            if action not in (IDLE_ACTION, SKIP_ACTION)
        }

    def _take_complete_round_locked(self) -> dict[str, str] | None:
        """Return the round's actions when every acting player has submitted.

        A round made up entirely of disconnect-idle placeholders never resolves:
        the story waits for at least one real player action.
        """
        acting = [cid for cid, player in self.players.items() if player.character_name is not None]
        if not acting or len(self.round_buffer) != len(acting):
            return None
        if all(action == IDLE_ACTION for action in self.round_buffer.values()):
            self.round_buffer.clear()
            return None
        self.state = GameState.AWAITING_LLM
        return dict(self.round_buffer)

    def _launch_round_locked(self, actions: dict[str, str], *, retry: bool = False) -> None:
        """Prepare and launch a round resolution job from buffered actions."""
        if not retry:
            participants = {
                item: (
                    self.players[item].name,
                    self.players[item].character_name or self.players[item].name,
                    self.players[item].departure_pending,
                    self.players[item].return_pending,
                    self.players[item].handover_pending,
                    self.players[item].connection_version,
                )
                for item in actions
                if self.players[item].is_connected
                or self.players[item].departure_pending
                or self.players[item].return_pending
                or self.players[item].handover_pending
            }
            self.pending_resolution = {
                "actions": actions,
                "participants": participants,
                "dice": None,
                "hidden": set(),
                "previous_state": self.current_scenario_state or "",
                "event_context": "\n\n".join(self.pending_event_injections),
            }
        self._launch_job_locked(self._resolve_round, GameState.AWAITING_LLM)

    async def _retry_round(self, client_id: str, data: dict[str, object]) -> None:
        """Re-run a paused round with its retained actions and dice."""
        del data
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            player = self.players.get(client_id)
            if player is None or not player.is_host:
                raise ValueError("Only the host can retry a paused round.")
            if not self.round_paused or self.pending_resolution is None:
                raise ValueError("No paused round to retry.")
            if self.inference_task is not None and not self.inference_task.done():
                raise ValueError("The previous request is still finishing.")
            self._launch_round_locked(self.round_buffer.copy(), retry=True)
        LOGGER.info("Paused round retry accepted round=%d", self.round_counter + 1)

    async def _resolve_round(self, epoch: int) -> None:
        """Measure all round work, including failed host attempts."""
        begin_usage = getattr(self.resolver, "begin_round_usage", None)
        if begin_usage is not None:
            begin_usage(self.round_counter + 1)
        error = None
        try:
            await self._resolve_round_work(epoch)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            finish_usage = getattr(self.resolver, "finish_round_usage", None)
            if finish_usage is not None:
                finish_usage(error)

    async def _resolve_round_work(self, epoch: int) -> None:
        """Run dice planning and resolution, then commit the round outcome."""
        pending = self.pending_resolution
        assert pending is not None
        actions = pending["actions"]
        participants = pending["participants"]
        llm_actions = {}
        for item, (human, name, departed, returned, handover, _) in participants.items():
            notes = []
            if departed:
                notes.append("[SYSTEM: Explain this player's in-world departure or inaction.]")
            if returned:
                notes.append("[SYSTEM: Explain this player's in-world return.]")
            if handover:
                notes.append(HANDOVER_NOTE)
            player = self.players.get(item)
            if player is not None:
                notes.extend(player.catchup_notes)
            if actions[item] not in (IDLE_ACTION, SKIP_ACTION):
                previous = self.previous_actions.get(name)
                if previous is not None and actions[item].strip() == previous.strip():
                    notes.append(REPEATED_ACTION_NOTE)
            llm_actions[name] = " ".join([*notes, actions[item]])
        plan_dice = getattr(self.resolver, "plan_dice", None)
        reused_dice = pending["dice"] is not None
        if pending["dice"] is None:
            pending["dice"] = {}
            if plan_dice is not None:
                # No fallback to unchecked resolution: a failed plan pauses the round.
                pending["dice"] = None
                plan = await plan_dice(llm_actions, pending["previous_state"])
                if set(plan.rolls) != set(llm_actions) or not set(plan.hidden_rolls) <= {
                    name for name, required in plan.rolls.items() if required
                }:
                    raise LLMResolutionError("Invalid dice plan participants.")
                pending["hidden"] = set(plan.hidden_rolls)
                pending["dice"] = {
                    name: roll_d100() for name, required in plan.rolls.items() if required
                }
        if self.private_guidance:
            LOGGER.info(
                "Private guidance checks round=%d generation=%d reused=%s rolls=%s",
                self.round_counter + 1,
                epoch,
                reused_dice,
                json.dumps(
                    {name: pending["dice"][name] for name in sorted(pending["hidden"])},
                    ensure_ascii=True,
                ),
            )
        if plan_dice is None:
            resolution = await self.resolver.generate_resolution(
                llm_actions,
                current_time=self._authoritative_time_label(),
                event_context=pending.get("event_context", ""),
            )
        else:
            resolution = await self.resolver.generate_resolution(
                llm_actions,
                pending["dice"],
                hidden_rolls=pending["hidden"],
                current_time=self._authoritative_time_label(),
                event_context=pending.get("event_context", ""),
            )
        if (
            set(resolution.player_resolutions) != set(llm_actions)
            or not resolution.global_narrative.strip()
            or any(not text.strip() for text in resolution.player_resolutions.values())
        ):
            raise LLMResolutionError("Invalid resolution participants or empty narrative.")
        public_dice = {
            name: value for name, value in pending["dice"].items() if name not in pending["hidden"]
        }
        display_actions = {
            name: (SKIP_DISPLAY if actions[item] == SKIP_ACTION else actions[item])
            for item, (_, name, _, _, _, _) in participants.items()
        }
        outcomes = {}
        for item, (_, name, _, _, _, _) in participants.items():
            result = resolution.player_resolutions.get(
                name, resolution.player_resolutions.get(item, "No resolution was provided.")
            )
            outcomes[name] = name_resolution(name, result)
        display = resolution.model_copy(
            update={"round_title": None, "player_resolutions": outcomes}
        )
        # Capture per-participant display data before skipped players are removed.
        colors = {
            self.players[item].character_name
            or self.players[item].name: self.players[item].join_index
            for item in participants
        }
        previous_by_name = {
            self.players[item].character_name or self.players[item].name: action
            for item, action in actions.items()
        }
        async with self.effects_lock:
            async with self.lock:
                if not self._job_current(epoch):
                    return
                self.round_counter += 1
                number = self.round_counter
                for item, (_, _, departed, returned, handover, version) in participants.items():
                    player = self.players[item]
                    if player.connection_version == version:
                        if departed:
                            player.departure_pending = False
                        if returned:
                            player.return_pending = False
                        if handover:
                            player.handover_pending = False
                        player.catchup_notes.clear()
                # Mid-game joiners become players for the next round.
                for pending_id, pending_player in list(self.pending_players.items()):
                    self.players[pending_id] = pending_player
                    self.join_order.append(pending_id)
                self.pending_players.clear()
                # Skipped players are removed from the game and must rejoin.
                kicked: list[tuple[str, str, str]] = []
                for item, (_, _, _, _, _, version) in participants.items():
                    player = self.players[item]
                    if player.skip_pending and player.connection_version == version:
                        kicked.append(
                            (
                                item,
                                player.name,
                                player.character_name or player.name,
                            )
                        )
                for item, _, character in kicked:
                    self.players.pop(item, None)
                    if item in self.join_order:
                        self.join_order.remove(item)
                    self.claims.pop(character, None)
                    LOGGER.info(
                        "Skipped player removed room=%s player=%s character=%s",
                        self.room_code,
                        item,
                        character,
                    )
                self.current_scenario_state = resolution.global_narrative
                # Event injections consumed by this round's request are spent.
                self.pending_event_injections = []
                time_elapsed = None
                event_announcements: list[str] = []
                if self.time_enabled:
                    elapsed = resolution.time_elapsed_minutes
                    if elapsed is None:
                        elapsed = self.default_elapsed_minutes
                        LOGGER.info(
                            "Missing time estimate round=%d; using default %d minutes",
                            number,
                            elapsed,
                        )
                    elif elapsed > self.max_elapsed_minutes:
                        LOGGER.info(
                            "Oversized time estimate round=%d minutes=%d; clamped to %d",
                            number,
                            elapsed,
                            self.max_elapsed_minutes,
                        )
                        elapsed = self.max_elapsed_minutes
                    elapsed = max(0, elapsed)
                    self.game_clock.add_minutes(elapsed)
                    time_elapsed = elapsed
                    # Fixed-time events fire deterministically once the clock passes.
                    total = self.game_clock.day * GameClock.MINUTES_PER_DAY + self.game_clock.minute
                    for event in self.timed_events:
                        if event.name in self.fired_events:
                            continue
                        due = event.day * GameClock.MINUTES_PER_DAY + event.minute
                        if total >= due:
                            self.fired_events.add(event.name)
                            fired_at = self.game_clock.format()
                            self.event_log.append(
                                {
                                    "name": event.name,
                                    "description": event.description,
                                    "public": event.public,
                                    "fired_at": fired_at,
                                    "fired_total_minutes": total,
                                }
                            )
                            self.pending_event_injections.append(
                                f"[SYSTEM Scheduled event fired at {fired_at}] "
                                f"{event.name}: {event.description} This event now occurs for "
                                "every online player at the same time. Narrate its consequences "
                                "in this round's outcomes."
                            )
                            if event.public:
                                event_announcements.append(
                                    f"全局事件「{event.name}」: {event.description}"
                                )
                            LOGGER.info(
                                "Scheduled event fired room=%s event=%s at=%s",
                                self.room_code,
                                event.name,
                                fired_at,
                            )
                self.round_buffer.clear()
                self.previous_actions = previous_by_name
                self.turn_queue = deque(self.join_order)
                self._shuffle_turn_queue_locked()
                self.pending_resolution = None
                self.state = GameState.ACTIVE_TURN
                directive = self._next_turn_locked()
                # Player-safe public history for rejoining clients; hidden dice
                # and private guidance never enter these records.
                history_record = {
                    "round_number": number,
                    "game_time": self._authoritative_time_label(),
                    "player_order": [
                        (self.players[item].character_name or self.players[item].name)
                        for item in self.join_order
                    ],
                    "actions": {
                        name: action
                        for name, action in display_actions.items()
                        if action not in (IDLE_ACTION, SKIP_DISPLAY)
                    },
                    "global_narrative": resolution.global_narrative,
                    "player_resolutions": outcomes,
                    "dice_results": public_dice,
                }
                self.round_history.append(history_record)
                if len(self.round_history) > MAX_ROUND_HISTORY:
                    del self.round_history[: len(self.round_history) - MAX_ROUND_HISTORY]
            try:
                await self.transcript.append_round(
                    number,
                    display_actions,
                    display,
                    public_dice,
                    player_colors=colors,
                    hidden_dice_results={
                        name: value
                        for name, value in pending["dice"].items()
                        if name in pending["hidden"]
                    },
                )
            except OSError:
                LOGGER.warning("Could not append round %s to transcript", number)
            compact_code = self.room_code.replace("-", "") if self.room_code else ""
            await self.history_store.append(compact_code, history_record)
            payload = display.model_dump()
            payload.update(
                round_number=number,
                game_time=self._authoritative_time_label(),
                time_elapsed_minutes=time_elapsed,
                submitted_actions={
                    name: action
                    for name, action in display_actions.items()
                    if action not in (IDLE_ACTION, SKIP_DISPLAY)
                },
                player_order=[
                    (self.players[item].character_name or self.players[item].name)
                    for item in self.join_order
                ],
                dice_results=public_dice,
            )
            await self.sender.broadcast_global(ServerEvent(type="state_update", payload=payload))
            for announcement in event_announcements:
                await self.sender.broadcast_global(
                    ServerEvent(type="system_msg", payload={"msg": announcement})
                )
            await self._publish_usage()
            if kicked:
                for item, human, character in kicked:
                    await self.sender.send_personal(
                        item,
                        ServerEvent(
                            type="removed",
                            payload={
                                "msg": (
                                    "You were removed from the game by a unanimous skip "
                                    "vote. Rejoin the room to continue playing."
                                )
                            },
                        ),
                    )
                await self.sender.broadcast_global(self._player_roster_event())
            if directive is not None:
                await self.sender.broadcast_global(
                    ServerEvent(type="round_start", payload={"round_number": number + 1})
                )
                await self.sender.broadcast_global(directive)

    async def _request_history(self, client_id: str, data: dict[str, object]) -> None:
        """Serve an older page of the room's public round history."""
        before_raw = data.get("before_round")
        if before_raw is not None and (
            isinstance(before_raw, bool) or not isinstance(before_raw, int)
        ):
            raise ValueError("'before_round' must be an integer.")
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            player = self.players.get(client_id) or self.pending_players.get(client_id)
            if player is None or not player.is_connected:
                raise ValueError("Authenticate before requesting history.")
        compact_code = self.room_code.replace("-", "") if self.room_code else ""
        rounds, has_more = await self.history_store.load_before(compact_code, before_raw)
        await self.sender.send_personal(
            client_id,
            ServerEvent(type="history_chunk", payload={"rounds": rounds, "has_more": has_more}),
        )

    async def _publish_usage(self, client_id: str | None = None) -> None:
        """Broadcast or send the latest token usage event."""
        tokens = getattr(self.resolver, "last_token_usage", None)
        if tokens is None:
            return
        refresh = getattr(self.resolver, "refresh_usage", None)
        if refresh is not None:
            await refresh()
        snapshot = getattr(self.resolver, "usage_snapshot", None)
        event = ServerEvent(
            type="token_usage",
            payload={
                "approximate_tokens": tokens,
                "context_window_size": getattr(self.resolver, "context_window_size", 8_192),
                "counting_method": getattr(self.resolver, "token_count_method", "estimate"),
                **(snapshot() if snapshot is not None else {}),
            },
        )
        if client_id is None:
            await self.sender.broadcast_global(event)
        else:
            await self.sender.send_personal(client_id, event)

    async def _send_error(self, client_id: str, message: str) -> None:
        """Send a personal error event to a client."""
        await self.sender.send_personal(
            client_id,
            ServerEvent(type="error", payload={"msg": message, "state": self.state.name}),
        )


def restore_engine(
    data: dict[str, object],
    sender: EventSender,
    resolver_factory: Callable[[], ResolutionManager],
) -> GameEngine:
    """Rebuild a game engine from a persisted room dict."""
    engine = GameEngine(sender, resolver_factory())
    state_name = str(data.get("state", GameState.AWAITING_HOST.name))
    engine.state = GameState[state_name]
    if engine.state in {GameState.ACTIVE_TURN, GameState.AWAITING_LLM}:
        # Resume at a fresh turn; any in-flight round is simply restarted.
        engine.state = GameState.ACTIVE_TURN
    engine.cast = [Character(**char) for char in data.get("cast", [])]
    engine.claims = dict(data.get("claims", {}))
    players: dict[str, Player] = {}
    for entry in data.get("players", []):
        player = Player(
            client_id=str(entry["client_id"]),
            name=str(entry["name"]),
            is_host=bool(entry["is_host"]),
            join_index=int(entry.get("join_index", 0)),
            character_name=entry.get("character_name"),
            reconnect_token=str(entry["reconnect_token"]),
        )
        player.connection_version = int(entry.get("connection_version", 0))
        player.last_seen_total = entry.get("last_seen_total")
        player.is_connected = False
        players[player.client_id] = player
    engine.players = players
    engine.join_order = [str(item) for item in data.get("join_order", [])]
    engine.turn_queue = deque(engine.join_order)
    engine.random_turn_order = bool(data.get("random_turn_order", True))
    engine._shuffle_turn_queue_locked()
    engine.host_client_id = data.get("host_client_id")
    engine.scenario_title = data.get("scenario_title")
    engine.original_scenario = data.get("original_scenario")
    engine.private_guidance = str(data.get("private_guidance", ""))
    engine.current_scenario_state = data.get("current_scenario_state")
    engine.opening_scenario = data.get("opening_scenario")
    engine.round_counter = int(data.get("round_counter", 0))
    engine.previous_actions = {str(k): str(v) for k, v in data.get("previous_actions", {}).items()}
    engine.time_enabled = bool(data.get("time_enabled", False))
    engine.game_clock = GameClock.from_dict(data.get("game_clock"))
    engine.max_elapsed_minutes = int(data.get("max_elapsed_minutes", 600))
    engine.default_elapsed_minutes = int(data.get("default_elapsed_minutes", 15))
    engine.time_rules = [
        TimeRule(**rule) for rule in data.get("time_rules", []) if isinstance(rule, dict)
    ]
    engine.timed_events = [
        TimedEvent(**event) for event in data.get("timed_events", []) if isinstance(event, dict)
    ]
    engine.fired_events = {str(name) for name in data.get("fired_events", [])}
    engine.event_log = [
        dict(entry) for entry in data.get("event_log", []) if isinstance(entry, dict)
    ]
    engine.lorebook = [
        LorebookEntry(**entry) for entry in data.get("lorebook", []) if isinstance(entry, dict)
    ]
    engine.prompt_blocks = [
        PromptBlock(**block) for block in data.get("prompt_blocks", []) if isinstance(block, dict)
    ]
    engine.sampling = SamplingConfig(**data.get("sampling", {}))
    engine.round_history = [
        dict(record) for record in data.get("round_history", []) if isinstance(record, dict)
    ]
    transcript_path = data.get("transcript_path")
    if transcript_path:
        engine.transcript.path = Path(str(transcript_path))
        engine.transcript._finalized = False
    restore_resolver = getattr(engine.resolver, "restore_from_persistent_dict", None)
    if restore_resolver is not None:
        restore_resolver(data.get("resolver") or {})
    set_lorebook = getattr(engine.resolver, "set_lorebook", None)
    if set_lorebook is not None:
        set_lorebook(engine.lorebook)
    set_cast = getattr(engine.resolver, "set_cast", None)
    if set_cast is not None:
        set_cast(engine.cast)
    set_prompt_blocks = getattr(engine.resolver, "set_prompt_blocks", None)
    if set_prompt_blocks is not None:
        set_prompt_blocks(engine.prompt_blocks)
    set_sampling = getattr(engine.resolver, "set_sampling", None)
    if set_sampling is not None:
        set_sampling(engine.sampling)
    engine.active_player_id = None
    engine.round_buffer = {}
    engine.pending_resolution = None
    engine.round_paused = False
    return engine

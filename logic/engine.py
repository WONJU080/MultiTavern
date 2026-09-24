"""Atomic game state with owned, cancellable inference tasks."""

import asyncio
import json
import logging
from collections import deque
from collections.abc import Awaitable, Callable

from core.config import settings
from core.schemas import Character, ServerEvent
from logic.dice import roll_d100
from logic.llm_manager import LLMBackendUnavailableError, LLMResolutionError
from logic.lobby import CURRENT_OWNER, LobbyMixin
from logic.models import EventSender, GameState, Player, ResolutionManager
from logic.presentation import name_resolution
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
        self.transcript = GameTranscript()

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
        if close_resolver:
            close = getattr(self.resolver, "close", None)
            if close is not None:
                await close()

    def connection_version_of(self, client_id: str) -> int | None:
        """Return the connection version of a player or pending joiner."""
        player = self.players.get(client_id) or self.pending_players.get(client_id)
        return player.connection_version if player is not None else None

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
                if player_id != target and player.is_connected
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
            player = self.players[client_id]
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
        """Return the round's actions when every player has submitted."""
        if not self.players or len(self.round_buffer) != len(self.players):
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
            resolution = await self.resolver.generate_resolution(llm_actions)
        else:
            resolution = await self.resolver.generate_resolution(
                llm_actions, pending["dice"], hidden_rolls=pending["hidden"]
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
                self.round_buffer.clear()
                self.previous_actions = previous_by_name
                self.turn_queue = deque(self.join_order)
                self.pending_resolution = None
                self.state = GameState.ACTIVE_TURN
                directive = self._next_turn_locked()
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
            payload = display.model_dump()
            payload.update(
                round_number=number,
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

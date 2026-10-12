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
    Item,
    LorebookEntry,
    PossibleTruthsConfig,
    PromptBlock,
    Region,
    RegionLink,
    RoomPlugin,
    SamplingConfig,
    ServerEvent,
    TimedEvent,
    TimeRule,
    TruthQuestion,
)
from logic.dice import roll_d100
from logic.game_clock import GameClock
from logic.llm_manager import (
    LLMBackendUnavailableError,
    LLMKeyRequiredError,
    LLMResolutionError,
)
from logic.lobby import CURRENT_OWNER, LobbyMixin
from logic.models import EventSender, GameState, Player, ResolutionManager
from logic.plugins import newly_revealed, truths_config
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
WAIT_UNIFY_MINUTES = 15


def _collapse_whitespace(text: str) -> str:
    """Remove all whitespace so verbatim quotes can be checked reliably."""
    return "".join(text.split())


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
        "truth_request": "_truth_request",
        "truth_submit": "_truth_submit",
        "hold": "_hold",
        "map_request": "_map_request",
        "codex_request": "_codex_request",
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
        self.round_durations: dict[str, int] = {}
        self.pending_proposals: list[dict[str, object]] = []
        self.agreed_plans: list[dict[str, object]] = []
        self.plan_counter = 0
        self.previous_actions: dict[str, str] = {}
        # Strict (per-action) scheduling state.
        self.schedule_mode = "round"
        self.inflight: dict[str, dict[str, object]] = {}
        self.window: set[str] = set()
        self.holds: set[str] = set()
        # Region/item world state.
        self.regions: dict[str, Region] = {}
        self.items: dict[str, Item] = {}
        self.world_start_region = ""
        self.region_of: dict[str, str] = {}
        self.visited_regions: set[str] = set()
        self.region_flags: set[str] = set()
        self.item_location: dict[str, str] = {}
        self.investigated_items: set[str] = set()
        self.region_state: dict[str, str] = {}
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
        self.pending_schedule: dict | None = None
        self.round_counter = 0
        self.scenario_title: str | None = None
        self.original_scenario: str | None = None
        self.fixed_opening = ""
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
        self.plugins: list[RoomPlugin] = []
        self.truth_revealed: set[str] = set()
        self.truth_answers: dict[str, list[dict[str, str]]] = {}
        self.truth_used_round: dict[str, int] = {}
        self.truth_pending: set[str] = set()
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

    def _truths_plugin(self) -> PossibleTruthsConfig | None:
        """Return the enabled possible-truths configuration, if any."""
        return truths_config(self.plugins)

    def _current_truth_round_locked(self) -> int:
        """Return the round number a submission would belong to."""
        if self.state in {GameState.ACTIVE_TURN, GameState.AWAITING_LLM}:
            return self.round_counter + 1
        return self.round_counter

    def _refresh_truth_reveals_locked(self) -> list[TruthQuestion]:
        """Reveal questions whose conditions are now satisfied; must hold the lock."""
        config = self._truths_plugin()
        if config is None:
            return []
        revealed = newly_revealed(
            config,
            self.truth_revealed,
            current_round=self._current_truth_round_locked(),
            time_total=self._clock_total() if self.time_enabled else None,
            fired_events=self.fired_events,
        )
        for question in revealed:
            self.truth_revealed.add(question.id)
            self.pending_event_injections.append(
                "[SYSTEM: 适时让相关人物向调查员提出、或让调查员自然发现以下疑问；不要照读"
                f"系统措辞，也不要标注来源：{question.text}]"
            )
        return revealed

    def _truth_context(self) -> str:
        """Render collected truths for the DM request, honoring feed_dm."""
        config = self._truths_plugin()
        if config is None or not config.feed_dm:
            return ""
        lines = []
        for question in config.questions:
            if question.id not in self.truth_revealed:
                continue
            for index, entry in enumerate(self.truth_answers.get(question.id, []), start=1):
                lines.append(
                    f"- 提问「{question.text}」：{entry['character']} 的可能真相（{index}）："
                    f"{entry['text']}"
                )
        if not lines:
            return ""
        return (
            "玩家们提交的『可能真相』（供参考的世界观素材；不必全部采用，也不要在叙述中"
            "直接暴露投稿者）：\n" + "\n".join(lines)
        )

    def _truth_state_payload_locked(self, player: Player) -> dict[str, object]:
        """Build the per-player possible-truths snapshot."""
        config = self._truths_plugin()
        questions: list[dict[str, object]] = []
        if config is not None:
            for question in config.questions:
                if question.id not in self.truth_revealed:
                    continue
                questions.append(
                    {
                        "id": question.id,
                        "text": question.text,
                        "history": [
                            {"character": entry["character"], "text": entry["text"]}
                            for entry in self.truth_answers.get(question.id, [])
                        ],
                    }
                )
        effective_round = self._current_truth_round_locked()
        used = self.truth_used_round.get(player.name)
        return {
            "enabled": config is not None,
            "round": effective_round,
            "your_used_round": used if used == effective_round else None,
            "max_answer_chars": config.max_answer_chars if config is not None else 500,
            "questions": questions,
        }

    @staticmethod
    def _combined_event_context(pending: dict) -> str:
        """Merge scheduled-event injections with collected truths for the request."""
        return "\n\n".join(
            part
            for part in (pending.get("event_context", ""), pending.get("truth_context", ""))
            if part
        )

    async def _collect_action_truth_answers(
        self, participants: dict, actions: dict[str, str]
    ) -> list[tuple[str, str, str]]:
        """Detect actions that answer revealed questions; keep verbatim player quotes."""
        config = self._truths_plugin()
        if config is None:
            return []
        questions = [
            (question.id, question.text)
            for question in config.questions
            if question.id in self.truth_revealed
        ]
        if not questions:
            return []
        raw_actions = {
            name: actions[item]
            for item, (_, name, _, _, _, _) in participants.items()
            if actions.get(item) not in (IDLE_ACTION, SKIP_ACTION)
        }
        if not raw_actions:
            return []
        extract = getattr(self.resolver, "extract_truth_answers", None)
        if extract is None:
            return []
        try:
            result = await extract(questions, raw_actions)
        except Exception:
            LOGGER.exception("Truth extraction failed room=%s", self.room_code)
            return []
        findings = getattr(result, "findings", result) or []
        valid_questions = {question_id for question_id, _ in questions}
        seen: set[tuple[str, str, str]] = set()
        collected: list[tuple[str, str, str]] = []
        for finding in findings:
            question_id = str(getattr(finding, "question_id", ""))
            character = str(getattr(finding, "character", ""))
            quote = str(getattr(finding, "quote", "")).strip()
            if question_id not in valid_questions or not quote:
                continue
            action_text = raw_actions.get(character)
            if not action_text or _collapse_whitespace(quote) not in _collapse_whitespace(
                action_text
            ):
                LOGGER.info(
                    "Discarding non-verbatim truth answer room=%s question=%s character=%s",
                    self.room_code,
                    question_id,
                    character,
                )
                continue
            quote = quote[: config.max_answer_chars]
            key = (question_id, character, quote)
            if key in seen:
                continue
            seen.add(key)
            collected.append(key)
        return collected

    async def _announce_truth_reveals(self, revealed: list[TruthQuestion]) -> None:
        """Broadcast newly revealed questions to the room."""
        for question in revealed:
            await self.sender.broadcast_global(
                ServerEvent(
                    type="truth_question",
                    payload={"id": question.id, "text": question.text},
                )
            )

    async def _truth_request(self, client_id: str, data: dict[str, object]) -> None:
        """Send the requesting player the current possible-truths state."""
        del data
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            player = self.players.get(client_id)
            if player is None:
                raise ValueError("只有本局玩家可以查看『可能的真相』。")
            payload = self._truth_state_payload_locked(player)
        await self.sender.send_personal(client_id, ServerEvent(type="truth_state", payload=payload))

    async def _truth_submit(self, client_id: str, data: dict[str, object]) -> None:
        """Validate one possible-truth submission and broadcast it when accepted."""
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            player = self.players.get(client_id)
            if player is None or not player.is_connected:
                raise ValueError("只有在线玩家可以提交『可能的真相』。")
            if not player.character_name:
                raise ValueError("请先选择角色后再提交。")
            if self.state not in {GameState.ACTIVE_TURN, GameState.AWAITING_LLM}:
                raise ValueError("游戏尚未开始或已经结束。")
            config = self._truths_plugin()
            if config is None:
                raise ValueError("本房间未启用『可能的真相』。")
            question_id = data.get("question_id")
            question = next((item for item in config.questions if item.id == question_id), None)
            if question is None or question.id not in self.truth_revealed:
                raise ValueError("该提问不存在或尚未揭晓。")
            round_number = self._current_truth_round_locked()
            if self.truth_used_round.get(player.name) == round_number:
                raise ValueError("本轮已经提交过了，请等下一轮。")
            if player.name in self.truth_pending:
                raise ValueError("你上一次提交还在判定中，请稍候。")
            try:
                answer = clean_text(data.get("answer"), "truth answer", config.max_answer_chars)
            except ValueError:
                raise ValueError(f"真相内容不能为空，且不能超过 {config.max_answer_chars} 字。")
            character = player.character_name
            self.truth_pending.add(player.name)
        try:
            try:
                verdict = await self.resolver.evaluate_truth(
                    question.text, answer, config.require_declarative
                )
            except Exception:
                LOGGER.exception(
                    "Truth evaluation failed room=%s player=%s", self.room_code, player.name
                )
                await self.sender.send_personal(
                    client_id,
                    ServerEvent(
                        type="truth_result",
                        payload={"valid": None, "reason": "裁判暂时不可用，请稍后重试。"},
                    ),
                )
                return
            valid = bool(getattr(verdict, "valid", False))
            reason = str(getattr(verdict, "reason", ""))[:200]
        finally:
            async with self.lock:
                self.truth_pending.discard(player.name)
        entry = None
        async with self.lock:
            current = self.players.get(client_id)
            if current is None or self.state not in {
                GameState.ACTIVE_TURN,
                GameState.AWAITING_LLM,
            }:
                return
            if self.truth_used_round.get(player.name) == round_number:
                return
            self.truth_used_round[player.name] = round_number
            if valid:
                entry = {"character": current.character_name or character, "text": answer}
                self.truth_answers.setdefault(question.id, []).append(entry)
        await self.sender.send_personal(
            client_id,
            ServerEvent(type="truth_result", payload={"valid": valid, "reason": reason}),
        )
        if entry is not None:
            await self.sender.broadcast_global(
                ServerEvent(
                    type="truth_answer",
                    payload={
                        "question_id": question.id,
                        "character": entry["character"],
                        "text": entry["text"],
                        "round": round_number,
                    },
                )
            )

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
            room_timeout = getattr(
                self.resolver, "request_timeout_seconds", settings.llm.request_timeout_seconds
            )
            async with asyncio.timeout(room_timeout * 3):
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
                "无法连接 AI 后端，请确认模型服务已运行且 endpoint 可访问后再重试。 "
                if isinstance(exc, LLMBackendUnavailableError)
                else ""
            )
            if isinstance(exc, LLMKeyRequiredError):
                connection_hint = "该房间没有可用的 API Key，请房主提供后再继续。 "
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
                                    "本轮已暂停，行动与骰点均已保留，房主可以重试或结束。"
                                    if self.round_paused
                                    else "无法准备游戏，请再试一次。"
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
            "fixed_opening": self.fixed_opening,
            "private_guidance": self.private_guidance,
            "current_scenario_state": self.current_scenario_state,
            "opening_scenario": self.opening_scenario,
            "round_counter": self.round_counter,
            "previous_actions": dict(self.previous_actions),
            "random_turn_order": self.random_turn_order,
            "schedule_mode": self.schedule_mode,
            "regions": [region.model_dump() for region in self.regions.values()],
            "items": [item.model_dump() for item in self.items.values()],
            "world_start_region": self.world_start_region,
            "region_of": dict(self.region_of),
            "visited_regions": sorted(self.visited_regions),
            "region_flags": sorted(self.region_flags),
            "item_location": dict(self.item_location),
            "investigated_items": sorted(self.investigated_items),
            "region_state": dict(self.region_state),
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
            "plugins": [plugin.model_dump() for plugin in self.plugins],
            "truth_revealed": sorted(self.truth_revealed),
            "truth_answers": {
                question_id: [dict(entry) for entry in entries]
                for question_id, entries in self.truth_answers.items()
            },
            "truth_used_round": dict(self.truth_used_round),
            "pending_proposals": [dict(proposal) for proposal in self.pending_proposals],
            "agreed_plans": [dict(plan) for plan in self.agreed_plans],
            "plan_counter": self.plan_counter,
            "round_history": list(self.round_history),
            "transcript_path": str(self.transcript.path) if self.transcript.path else None,
            "resolver": serialize_resolver() if serialize_resolver is not None else {},
        }

    async def shutdown(
        self,
        *,
        close_resolver: bool = True,
        reason: str = "房主结束了本局游戏。",
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
                self._drop_proposals_for(player.character_name)
                if self.time_enabled:
                    player.last_seen_total = (
                        self.game_clock.day * GameClock.MINUTES_PER_DAY + self.game_clock.minute
                    )
                directive = None
                window_events: list[ServerEvent] = []
                if self.state is GameState.ACTIVE_TURN:
                    if self.schedule_mode == "strict":
                        self.window.discard(client_id)
                        self.holds.discard(client_id)
                        if not self.window:
                            self._advance_schedule_locked()
                        if self.state is GameState.ACTIVE_TURN:
                            window_events = self._window_events_locked()
                    else:
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
            ServerEvent(type="system_msg", payload={"msg": f"{player.name} 已断开连接。"})
        )
        await self.sender.broadcast_global(self._player_roster_event())
        if directive is not None:
            await self.sender.broadcast_global(directive)
        for event in window_events:
            await self.sender.broadcast_global(event)

    def _action_allowed(self, client_id: str) -> None:
        """Raise if the client cannot submit an action in the current state."""
        player = self.players.get(client_id)
        if player is None or not player.is_connected:
            raise ValueError("请先加入房间再提交行动。")
        if self.state is not GameState.ACTIVE_TURN:
            raise ValueError("当前没有进行中的回合，无法提交行动。")
        if client_id != self.active_player_id:
            raise ValueError("还没轮到你。")

    async def _submit_action(self, client_id: str, data: dict[str, object]) -> None:
        """Validate and buffer a player action, launching a round when complete."""
        action = clean_text(data.get("action"), "action", 50_000)
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            if self.schedule_mode == "strict":
                self._scheduled_action_allowed(client_id)
            else:
                self._action_allowed(client_id)
            epoch = self.generation
            actor = self.players[client_id]
            character = actor.character_name or actor.name
            candidate = dict(self.round_buffer)
            candidate[client_id] = action
            named = {
                self.players[item].character_name
                or self.players[item].name: candidate.get(item, IDLE_ACTION)
                for item in self.join_order
                if self.players[item].character_name is not None
            }
            current_state = self.current_scenario_state or ""
            world_context = self._world_context_locked(character)
            roster = [
                player.character_name
                for player in self.players.values()
                if player.character_name is not None
            ]
            obligations = [
                {"from": str(proposal["from"]), "content": str(proposal["content"])}
                for proposal in self.pending_proposals
                if proposal["to"] == character and not proposal["resolved"]
            ]
            plans = [
                {
                    "id": str(plan["id"]),
                    "content": str(plan["content"]),
                    "participants": list(plan["participants"]),
                }
                for plan in self.agreed_plans
                if plan["status"] != "done" and character in plan["participants"]
            ]
            time_rules = list(self.time_rules)
        preflight = getattr(self.resolver, "preflight_round", None)
        if preflight is not None:
            try:
                await preflight(named, current_state)
            except LLMResolutionError as exc:
                raise ValueError(str(exc)) from exc
        verdict = await self._review_action(
            character, action, roster, world_context, time_rules, obligations, plans
        )
        if verdict is not None and not bool(getattr(verdict, "valid", True)):
            reason = str(getattr(verdict, "reason", "")).strip()
            raise ValueError(reason or "行动描述不够具体，请写出角色具体做了什么后重新提交。")
        if self.schedule_mode == "strict":
            await self._submit_scheduled(client_id, epoch, character, action, roster, verdict)
        else:
            await self._submit_round(client_id, epoch, character, action, roster, verdict)

    async def _submit_round(
        self,
        client_id: str,
        epoch: int,
        character: str,
        action: str,
        roster: list[str],
        verdict: object | None,
    ) -> None:
        """Legacy path: buffer the action and resolve the whole round together."""
        async with self.effects_lock:
            async with self.lock:
                if not CURRENT_OWNER.get()() or epoch != self.generation:
                    return
                self._action_allowed(client_id)
                contract = self._apply_action_contract_locked(character, action, roster, verdict)
                duration = contract["duration"]
                player = self.players[client_id]
                self.round_buffer[client_id] = action
                if duration is not None:
                    self.round_durations[client_id] = duration
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
            LOGGER.info(
                "Player action accepted round=%d duration=%s",
                action_event.payload["round_number"],
                duration,
            )
            await self.sender.broadcast_global(action_event)
            if directive is not None:
                await self.sender.broadcast_global(directive)

    async def _submit_scheduled(
        self,
        client_id: str,
        epoch: int,
        character: str,
        action: str,
        roster: list[str],
        verdict: object | None,
    ) -> None:
        """Strict path: schedule the action for its own completion time."""
        async with self.effects_lock:
            async with self.lock:
                if not CURRENT_OWNER.get()() or epoch != self.generation:
                    return
                self._scheduled_action_allowed(client_id)
                contract = self._apply_action_contract_locked(character, action, roster, verdict)
                duration = contract["duration"]
                if duration is None:
                    duration = self.default_elapsed_minutes
                now = self._clock_total()
                self.inflight[client_id] = {
                    "action": action,
                    "character": character,
                    "start": now,
                    "duration": duration,
                    "end": now + duration,
                    "move_to": contract["move_to"],
                    "take_item": contract["take_item"],
                    "investigate": contract["investigate"],
                }
                self.window.discard(client_id)
                self.holds.discard(client_id)
                player = self.players[client_id]
                action_event = ServerEvent(
                    type="action_echo",
                    payload={
                        "round_number": self.round_counter + 1,
                        "player_name": character,
                        "player_color_index": player.join_index,
                        "action": action,
                        "eta_minutes": duration,
                        "ends_at_label": self._schedule_time_label(now + duration),
                    },
                )
                if not self.window:
                    self._advance_schedule_locked()
                events = self._window_events_locked() if self.state is GameState.ACTIVE_TURN else []
            LOGGER.info(
                "Scheduled action character=%s duration=%d ends=%d",
                character,
                duration,
                now + duration,
            )
            await self.sender.broadcast_global(action_event)
            for event in events:
                await self.sender.broadcast_global(event)

    def _scheduled_action_allowed(self, client_id: str) -> None:
        """Raise unless the client may act in the current strict window."""
        player = self.players.get(client_id)
        if player is None or not player.is_connected:
            raise ValueError("请先加入房间再提交行动。")
        if self.state is not GameState.ACTIVE_TURN:
            raise ValueError("现在不是行动窗口，请等待结算完成。")
        if client_id in self.inflight:
            raise ValueError("你的行动仍在进行中，请等待其结束。")
        if client_id not in self.window:
            raise ValueError("现在不是你的行动窗口，请等待。")

    async def _hold(self, client_id: str, data: dict[str, object]) -> None:
        """Release the current strict window without consuming in-game time."""
        del data
        async with self.effects_lock:
            async with self.lock:
                if not CURRENT_OWNER.get()():
                    return
                if self.schedule_mode != "strict":
                    raise ValueError("观望仅用于时间轴模式。")
                self._scheduled_action_allowed(client_id)
                self.holds.add(client_id)
                self.window.discard(client_id)
                if not self.window:
                    self._advance_schedule_locked()
                events = self._window_events_locked() if self.state is GameState.ACTIVE_TURN else []
            for event in events:
                await self.sender.broadcast_global(event)

    def _schedule_time_label(self, total: int) -> str:
        """Render an absolute minute count as a Day hh:mm label."""
        day, minute = divmod(total, GameClock.MINUTES_PER_DAY)
        return f"Day {day} {minute // 60:02d}:{minute % 60:02d}"

    def _window_events_locked(self) -> list[ServerEvent]:
        """Build the window/waiting broadcast for the strict scheduler."""
        now = self._clock_total()
        active_ids = [cid for cid in self.join_order if cid in self.window]
        active_names = [
            self.players[cid].character_name or self.players[cid].name for cid in active_ids
        ]
        waiting = []
        for cid, entry in self.inflight.items():
            player = self.players.get(cid)
            if player is None:
                continue
            waiting.append(
                {
                    "player_id": cid,
                    "name": entry["character"],
                    "action": entry["action"],
                    "ends_at_label": self._schedule_time_label(int(entry["end"])),
                    "eta_minutes": max(0, int(entry["end"]) - now),
                }
            )
        for cid in self.holds:
            player = self.players.get(cid)
            if player is not None:
                waiting.append(
                    {
                        "player_id": cid,
                        "name": player.character_name or player.name,
                        "action": None,
                        "ends_at_label": None,
                        "eta_minutes": None,
                    }
                )
        return [
            ServerEvent(
                type="turn_window",
                payload={
                    "round_number": self.round_counter + 1,
                    "active_player_ids": active_ids,
                    "active_player_names": active_names,
                    "waiting": waiting,
                    "game_time": self._authoritative_time_label(),
                    "player_order": [
                        self.players[item].character_name or self.players[item].name
                        for item in self.join_order
                    ],
                },
            )
        ]

    def _map_state_payload_locked(self) -> dict[str, object]:
        """Build the party-shared map: visited regions, paths, known items and packs."""
        carried_items = {
            item_id
            for item_id, holder in self.item_location.items()
            if item_id in self.items and holder not in self.regions
        }

        def link_open(link: RegionLink) -> bool:
            # A structured item gate is open when the party carries the item;
            # free-text requirements are narrative and not evaluated here.
            return not link.requires_item or link.requires_item in carried_items

        regions: list[dict[str, object]] = []
        for region in self.regions.values():
            if region.id not in self.visited_regions:
                continue
            items = []
            for item_id in region.items:
                item = self.items.get(item_id)
                if item is None or self.item_location.get(item_id) != region.id:
                    continue
                investigated = item.id in self.investigated_items
                items.append(
                    {
                        "id": item.id,
                        "name": item.name,
                        "summary": item.summary,
                        "clue": item.clue,
                        "carryable": item.carryable,
                        "investigated": investigated,
                        "detail": item.investigate_text if investigated else "",
                    }
                )
            reachable: list[dict[str, object]] = []
            locked: list[dict[str, object]] = []
            for link in region.links:
                target = self.regions.get(link.to)
                if target is None:
                    continue
                entry = {"id": target.id, "name": target.name}
                (reachable if link_open(link) else locked).append(entry)
            regions.append(
                {
                    "id": region.id,
                    "name": region.name,
                    "items": items,
                    "reachable": reachable,
                    "locked": locked,
                }
            )
        players = []
        for cid in self.join_order:
            player = self.players.get(cid)
            if player is None or player.character_name is None:
                continue
            carried = [
                self.items[item_id].name
                for item_id, holder in self.item_location.items()
                if holder == player.character_name and item_id in self.items
            ]
            entry = self.inflight.get(cid)
            transit = str(entry["move_to"]) if entry is not None and entry.get("move_to") else None
            players.append(
                {
                    "name": player.character_name,
                    "region": self.region_of.get(player.character_name, self.world_start_region),
                    "in_transit_to": transit,
                    "carried": carried,
                }
            )
        return {
            "start_region": self.world_start_region,
            "regions": regions,
            "players": players,
        }

    async def _map_request(self, client_id: str, data: dict[str, object]) -> None:
        """Send the requesting player the shared map snapshot."""
        del data
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            player = self.players.get(client_id) or self.pending_players.get(client_id)
            if player is None:
                raise ValueError("只有房间成员可以查看地图。")
            if not self.regions:
                raise ValueError("本房间没有启用地区系统。")
            payload = self._map_state_payload_locked()
        await self.sender.send_personal(client_id, ServerEvent(type="map_state", payload=payload))

    def _codex_state_payload_locked(self) -> dict[str, object]:
        """Build discovered items and fired events with their host-authored blurbs."""
        items = []
        for item in self.items.values():
            if item.backstage:
                continue
            location = str(self.item_location.get(item.id, ""))
            carried = bool(location) and location not in self.regions
            discovered = (
                item.id in self.investigated_items
                or (location in self.regions and location in self.visited_regions)
                or carried
            )
            if not discovered:
                continue
            origin = self.regions.get(item.region) if item.region else None
            items.append(
                {
                    "id": item.id,
                    "name": item.name,
                    "summary": item.summary,
                    "clue": item.clue,
                    "investigated": item.id in self.investigated_items,
                    "detail": item.investigate_text if item.id in self.investigated_items else "",
                    "origin": origin.name if origin is not None else "",
                    "here": carried,
                }
            )
        events = []
        for event in self.timed_events:
            if event.name not in self.fired_events:
                continue
            region = self.regions.get(event.region) if event.region else None
            events.append(
                {
                    "name": event.name,
                    "description": event.description,
                    "actor": event.actor,
                    "region": region.name if region is not None else "",
                    "public": event.public,
                }
            )
        return {"items": items, "events": events}

    async def _codex_request(self, client_id: str, data: dict[str, object]) -> None:
        """Send the requesting player the discovered items and fired events."""
        del data
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            player = self.players.get(client_id) or self.pending_players.get(client_id)
            if player is None:
                raise ValueError("只有房间成员可以查看图鉴。")
            payload = self._codex_state_payload_locked()
        await self.sender.send_personal(client_id, ServerEvent(type="codex_state", payload=payload))

    def _open_window_locked(self) -> list[ServerEvent]:
        """Open a fresh input window for every connected, idle character."""
        self.window = {
            cid
            for cid, player in self.players.items()
            if player.character_name is not None
            and cid not in self.inflight
            and player.is_connected
        }
        self.holds = set()
        return self._window_events_locked()

    def _unify_window_durations_locked(self) -> None:
        """Snap near-equal durations started in the same window to their maximum."""
        starts = {int(entry["start"]) for entry in self.inflight.values()}
        for start in starts:
            group = [cid for cid, entry in self.inflight.items() if int(entry["start"]) == start]
            durations = [int(self.inflight[cid]["duration"]) for cid in group]
            longest = max(durations)
            if all(longest - value <= WAIT_UNIFY_MINUTES for value in durations):
                for cid in group:
                    entry = self.inflight[cid]
                    entry["duration"] = longest
                    entry["end"] = start + longest

    def _advance_schedule_locked(self) -> None:
        """Advance the strict timeline to the next completion or world-event boundary.

        Time only moves while at least one action is running. If every player is
        idle (including those who held), the clock stays put and the window reopens.
        """
        if self.state is not GameState.ACTIVE_TURN:
            return
        self.window.clear()
        self.holds.clear()
        if not self.inflight:
            self._open_window_locked()
            return
        self._unify_window_durations_locked()
        now = self._clock_total()
        next_end = min(int(entry["end"]) for entry in self.inflight.values())
        next_event = self._next_time_event_total_locked(now)
        if next_event is not None and next_event < next_end:
            boundary = int(next_event)
            self._launch_job_locked(
                lambda epoch: self._resolve_event_boundary(epoch, boundary),
                GameState.AWAITING_LLM,
            )
            return
        completed = tuple(
            cid for cid, entry in self.inflight.items() if int(entry["end"]) <= next_end
        )
        self.pending_schedule = {"completed": completed, "next_end": next_end}
        self._launch_job_locked(
            lambda epoch: self._resolve_scheduled(epoch, completed, next_end),
            GameState.AWAITING_LLM,
        )

    def _next_time_event_total_locked(self, after: int) -> int | None:
        """Return the next unfired time-triggered event's absolute minute."""
        if not self.time_enabled:
            return None
        future = [
            event.day * GameClock.MINUTES_PER_DAY + event.minute
            for event in self.timed_events
            if (not event.region or event.when == "time") and event.name not in self.fired_events
        ]
        candidates = [value for value in future if value > after]
        return min(candidates) if candidates else None

    def _fire_time_events_locked(self) -> list[dict]:
        """Fire every unfired time event due at or before the current clock."""
        if not self.time_enabled:
            return []
        announcements: list[dict] = []
        total = self._clock_total()
        for event in self.timed_events:
            if event.when == "enter" or event.name in self.fired_events:
                continue
            due = event.day * GameClock.MINUTES_PER_DAY + event.minute
            if total < due:
                continue
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
            actor_label = f"「{event.actor}」" if event.actor else ""
            region_hint = ""
            if event.region:
                region = self.regions.get(event.region)
                region_hint = f"（地区：{region.name if region else event.region}）"
            self.pending_event_injections.append(
                f"[SYSTEM Scheduled event fired at {fired_at}] {actor_label}{event.name}"
                f"{region_hint}: {event.description} This event now occurs for every online "
                "player at the same time. Narrate its consequences in this round's outcomes."
            )
            if event.public:
                announcements.append(
                    {
                        "msg": f"{actor_label}事件「{event.name}」: {event.description}",
                        "region": event.region,
                    }
                )
            LOGGER.info(
                "Scheduled event fired room=%s event=%s at=%s", self.room_code, event.name, fired_at
            )
        return announcements

    def _region_audience_locked(self, region_id: str) -> set[str] | None:
        """Return client ids allowed to see a private region's content, or None for all."""
        region = self.regions.get(region_id)
        if region is None or not region.private:
            return None
        return self._region_occupants_locked(region_id)

    def _region_occupants_locked(self, region_id: str) -> set[str]:
        """Return connected client ids whose character is currently inside a region."""
        return {
            cid
            for cid, player in self.players.items()
            if player.character_name is not None
            and self.region_of.get(player.character_name) == region_id
        }

    def _apply_region_updates_locked(self, updates: dict[str, str] | None) -> None:
        """Validate and apply AI region-state deltas; drop invalid entries (T7)."""
        for key, value in (updates or {}).items():
            region_id = str(key).strip()
            text = str(value).strip()[:500]
            if region_id in self.regions and text:
                self.region_state[region_id] = text
            else:
                LOGGER.info(
                    "Dropped invalid region update room=%s region=%s", self.room_code, region_id
                )

    def _apply_world_effects_locked(self, entry: dict, announcements: list[dict]) -> list[dict]:
        """Apply one completed action's region/item effects; return fixed scene texts."""
        if not self.regions:
            return []
        character = str(entry.get("character") or "")
        if not character:
            return []
        current = self.region_of.get(character, self.world_start_region)
        scene: list[dict] = []
        investigate = entry.get("investigate")
        if investigate:
            item = self.items.get(str(investigate))
            if item is not None and item.id not in self.investigated_items:
                self.investigated_items.add(item.id)
                if item.investigate_text:
                    text = item.investigate_text
                    scene.append(
                        {
                            "text": text,
                            "region": current,
                            "character": character,
                            "kind": "investigate",
                        }
                    )
                    self.pending_event_injections.append(
                        "[SYSTEM 以下调查结果已原样展示给玩家，不要改写或重复叙述：\n" + text + "]"
                    )
        take_item = entry.get("take_item")
        if take_item and self.item_location.get(str(take_item)) == current:
            self.item_location[str(take_item)] = character
        move_to = entry.get("move_to")
        if move_to and str(move_to) in self.regions and str(move_to) != current:
            target = str(move_to)
            self.region_of[character] = target
            if target not in self.visited_regions:
                self.visited_regions.add(target)
                region = self.regions[target]
                if region.arrival_text:
                    scene.append(
                        {
                            "text": region.arrival_text,
                            "region": target,
                            "character": character,
                            "kind": "arrival",
                        }
                    )
                    self.pending_event_injections.append(
                        "[SYSTEM 以下到达文本已原样展示给玩家，不要改写或重复叙述：\n"
                        + region.arrival_text
                        + "]"
                    )
            for event in self.timed_events:
                if event.region != target or event.when != "enter":
                    continue
                if event.once and event.name in self.fired_events:
                    continue
                self.fired_events.add(event.name)
                self.region_flags.add(f"{target}:{event.name}")
                self.pending_event_injections.append(
                    f"[SYSTEM Scheduled event fired at {self.game_clock.format()}] {event.name}: "
                    f"{event.description} 该事件现在对在场角色发生，请在本轮结果中叙述其后果。"
                )
                if event.public:
                    announcements.append(
                        {
                            "msg": f"事件「{event.name}」: {event.description}",
                            "region": target,
                        }
                    )
                LOGGER.info(
                    "Region event fired room=%s event=%s region=%s",
                    self.room_code,
                    event.name,
                    target,
                )
        return scene

    async def _resolve_scheduled(
        self, epoch: int, completed: tuple[str, ...], next_end: int
    ) -> None:
        """Measure and run one strict completion boundary."""
        begin_usage = getattr(self.resolver, "begin_round_usage", None)
        if begin_usage is not None:
            begin_usage(self.round_counter + 1)
        error = None
        try:
            await self._resolve_scheduled_work(epoch, completed, next_end)
        except BaseException as exc:
            error = type(exc).__name__
            raise
        finally:
            finish_usage = getattr(self.resolver, "finish_round_usage", None)
            if finish_usage is not None:
                finish_usage(error)

    async def _resolve_event_boundary(self, epoch: int, boundary: int) -> None:
        """Advance the clock to a world-event boundary with no player action due."""
        async with self.effects_lock:
            async with self.lock:
                if not self._job_current(epoch):
                    return
                self.game_clock.add_minutes(max(0, boundary - self._clock_total()))
                announcements = self._fire_time_events_locked()
                self.state = GameState.ACTIVE_TURN
                # Continue the timeline: another event, a completion, or a window.
                self._advance_schedule_locked()
                window_events = (
                    self._window_events_locked() if self.state is GameState.ACTIVE_TURN else []
                )
                private_notes = [
                    (note, self._region_occupants_locked(str(note.get("region") or "")))
                    for note in announcements
                    if note.get("region")
                ]
            for message in announcements:
                if message.get("region"):
                    continue
                await self.sender.broadcast_global(
                    ServerEvent(type="system_msg", payload={"msg": message.get("msg", "")})
                )
            for note, audience in private_notes:
                for cid in audience:
                    await self.sender.send_personal(
                        cid,
                        ServerEvent(type="system_msg", payload={"msg": note.get("msg", "")}),
                    )
            for event in window_events:
                await self.sender.broadcast_global(event)
            await self._publish_usage()

    async def _resolve_scheduled_work(
        self, epoch: int, completed: tuple[str, ...], next_end: int
    ) -> None:
        """Resolve the actions completing at one boundary, then reopen the window."""
        async with self.lock:
            if not self._job_current(epoch):
                return
            llm_actions: dict[str, str] = {}
            participants: dict[str, tuple] = {}
            for cid in completed:
                entry = self.inflight.get(cid)
                player = self.players.get(cid)
                if entry is None or player is None:
                    continue
                name = player.character_name or player.name
                participants[cid] = (
                    player.name,
                    name,
                    player.departure_pending,
                    player.return_pending,
                    player.handover_pending,
                    player.connection_version,
                )
                llm_actions[name] = str(entry["action"])
            previous_state = self.current_scenario_state or ""
            inflight_notes = [
                f"{entry['character']} 正在进行的行动：{entry['action']}"
                for cid, entry in self.inflight.items()
                if cid not in completed
            ]
            event_context = "\n".join(
                part
                for part in (
                    "\n\n".join(self.pending_event_injections),
                    (
                        "[SYSTEM 这些行动仍在进行中，结果尚未落定：]\n" + "\n".join(inflight_notes)
                        if inflight_notes
                        else ""
                    ),
                    (
                        "世界分区状态（若有变化，请在 region_updates 中以 地区id→一句话新状态 "
                        "返回）：\n"
                        + "\n".join(
                            f"- {self.regions[rid].name}（{rid}）：{text}"
                            for rid, text in self.region_state.items()
                            if rid in self.regions
                        )
                        if self.region_state
                        else ""
                    ),
                )
                if part
            )
            plan_dice = getattr(self.resolver, "plan_dice", None)
        if not llm_actions:
            resolution = None
            dice: dict[str, int] = {}
            hidden: set[str] = set()
        else:
            dice = {}
            hidden = set()
            if plan_dice is not None:
                plan = await plan_dice(llm_actions, previous_state)
                if set(plan.rolls) != set(llm_actions) or not set(plan.hidden_rolls) <= {
                    name for name, required in plan.rolls.items() if required
                }:
                    raise LLMResolutionError("骰点计划参与者无效。")
                hidden = set(plan.hidden_rolls)
                dice = {name: roll_d100() for name, required in plan.rolls.items() if required}
            if plan_dice is None:
                resolution = await self.resolver.generate_resolution(
                    llm_actions,
                    current_time=self._schedule_time_label(next_end),
                    event_context=event_context,
                )
            else:
                resolution = await self.resolver.generate_resolution(
                    llm_actions,
                    dice,
                    hidden_rolls=hidden,
                    current_time=self._schedule_time_label(next_end),
                    event_context=event_context,
                )
            if (
                set(resolution.player_resolutions) != set(llm_actions)
                or not resolution.global_narrative.strip()
                or any(not text.strip() for text in resolution.player_resolutions.values())
            ):
                raise LLMResolutionError("结算参与者无效或全局叙述为空。")
        async with self.effects_lock:
            async with self.lock:
                if not self._job_current(epoch):
                    return
                self.game_clock.add_minutes(max(0, next_end - self._clock_total()))
                self.round_counter += 1
                number = self.round_counter
                self.pending_schedule = None
                completed_effects: list[dict] = []
                for cid in completed:
                    entry = self.inflight.pop(cid, None)
                    player = self.players.get(cid)
                    if player is not None:
                        player.departure_pending = False
                        player.return_pending = False
                        player.handover_pending = False
                        player.catchup_notes.clear()
                    if entry is not None:
                        completed_effects.append(entry)
                pending_added = bool(self.pending_players)
                for pending_id, pending_player in list(self.pending_players.items()):
                    self.players[pending_id] = pending_player
                    self.join_order.append(pending_id)
                    if self.regions and pending_player.character_name:
                        self.region_of.setdefault(
                            pending_player.character_name, self.world_start_region
                        )
                self.pending_players.clear()
                # Injections for the next boundary; this round's request is spent.
                self.pending_event_injections = []
                announcements = self._fire_time_events_locked()
                scene_texts: list[dict] = []
                for entry in completed_effects:
                    scene_texts.extend(self._apply_world_effects_locked(entry, announcements))
                action_regions = {
                    str(entry.get("character")): self.region_of.get(
                        str(entry.get("character")), self.world_start_region
                    )
                    for entry in completed_effects
                    if entry.get("character")
                }
                display_actions = dict(llm_actions)
                public_dice = {name: value for name, value in dice.items() if name not in hidden}
                display = None
                history_record = None
                colors = {
                    (self.players[cid].character_name or self.players[cid].name): (
                        self.players[cid].join_index
                    )
                    for cid in completed
                    if cid in self.players
                }
                if resolution is not None:
                    outcomes = {
                        name: name_resolution(name, resolution.player_resolutions.get(name, ""))
                        for name in llm_actions
                    }
                    display = resolution.model_copy(
                        update={"round_title": None, "player_resolutions": outcomes}
                    )
                    history_record = {
                        "round_number": number,
                        "game_time": self._authoritative_time_label(),
                        "player_order": [
                            self.players[item].character_name or self.players[item].name
                            for item in self.join_order
                        ],
                        "actions": dict(display_actions),
                        "global_narrative": resolution.global_narrative,
                        "player_resolutions": outcomes,
                        "dice_results": public_dice,
                    }
                    self.round_history.append(history_record)
                    if len(self.round_history) > MAX_ROUND_HISTORY:
                        del self.round_history[: len(self.round_history) - MAX_ROUND_HISTORY]
                    self.current_scenario_state = resolution.global_narrative
                    self._apply_region_updates_locked(resolution.region_updates)
                self.state = GameState.ACTIVE_TURN
                window_events = self._open_window_locked()
                payload = (
                    display.model_dump()
                    if display is not None
                    else {"global_narrative": "", "player_resolutions": {}}
                )
                payload.update(
                    round_number=number,
                    game_time=self._authoritative_time_label(),
                    time_elapsed_minutes=None,
                    player_order=[
                        self.players[item].character_name or self.players[item].name
                        for item in self.join_order
                    ],
                    dice_results=public_dice,
                )
            if display is not None and history_record is not None:
                try:
                    await self.transcript.append_round(
                        number,
                        display_actions,
                        display,
                        public_dice,
                        player_colors=colors,
                        hidden_dice_results={
                            name: value for name, value in dice.items() if name in hidden
                        },
                    )
                except OSError:
                    LOGGER.warning("Could not append round %s to transcript", number)
                compact_code = self.room_code.replace("-", "") if self.room_code else ""
                await self.history_store.append(compact_code, history_record)
            private_present = any(
                (self.regions.get(region_id) is not None and self.regions[region_id].private)
                for region_id in action_regions.values()
            )
            visible_outcomes = display.player_resolutions if display is not None else {}
            if private_present and visible_outcomes:
                for cid, member in list(self.players.items()):
                    if not member.is_connected:
                        continue
                    visible = {}
                    for name, text in visible_outcomes.items():
                        region_id = action_regions.get(name)
                        audience = self._region_audience_locked(region_id) if region_id else None
                        if audience is None or cid in audience:
                            visible[name] = text
                    personal = dict(payload)
                    personal["player_resolutions"] = visible
                    await self.sender.send_personal(
                        cid, ServerEvent(type="state_update", payload=personal)
                    )
            else:
                await self.sender.broadcast_global(
                    ServerEvent(type="state_update", payload=payload)
                )
            await self.sender.broadcast_global(
                ServerEvent(type="round_start", payload={"round_number": number + 1})
            )
            for scene in scene_texts:
                region_id = str(scene.get("region") or "")
                audience = self._region_audience_locked(region_id) if region_id else None
                if audience is None:
                    await self.sender.broadcast_global(
                        ServerEvent(type="scene_text", payload=scene)
                    )
                else:
                    for cid in audience:
                        await self.sender.send_personal(
                            cid, ServerEvent(type="scene_text", payload=scene)
                        )
            for note in announcements:
                region_id = str(note.get("region") or "")
                message = ServerEvent(type="system_msg", payload={"msg": note.get("msg", "")})
                if region_id:
                    for cid in self._region_occupants_locked(region_id):
                        await self.sender.send_personal(cid, message)
                else:
                    await self.sender.broadcast_global(message)
            for event in window_events:
                await self.sender.broadcast_global(event)
            if pending_added:
                await self.sender.broadcast_global(self._player_roster_event())
            await self._publish_usage()

    def _world_context_locked(self, character: str) -> str:
        """Render the acting character's structured world context for classification."""
        if not self.regions:
            return self.current_scenario_state or ""
        parts: list[str] = []
        if self.current_scenario_state:
            parts.append(self.current_scenario_state)
        region_id = self.region_of.get(character, self.world_start_region)
        region = self.regions.get(region_id)
        if region is not None:
            parts.append(f"当前地区：{region.name}（id={region.id}）")
            state = self.region_state.get(region.id)
            if state:
                parts.append(f"当前地区状态：{state}")
            visible = [
                f"{self.items[item_id].name}（id={item_id}）"
                + ("" if self.items[item_id].carryable else "（不可携带）")
                for item_id in region.items
                if self.item_location.get(item_id) == region.id and item_id in self.items
            ]
            if visible:
                parts.append("可见物品：" + "、".join(visible))
            links = []
            for link in region.links:
                target = self.regions.get(link.to)
                if target is None:
                    continue
                label = f"{target.name}（id={link.to}）"
                if link.requires:
                    label += f"[{link.requires}]"
                links.append(label)
            if links:
                parts.append("可前往：" + "、".join(links))
        carried = [
            f"{self.items[item_id].name}（id={item_id}）"
            for item_id, holder in self.item_location.items()
            if holder == character and item_id in self.items
        ]
        if carried:
            parts.append("携带物品：" + "、".join(carried))
        return "\n".join(parts)

    async def _review_action(
        self,
        character: str,
        action: str,
        roster: list[str],
        current_state: str,
        time_rules: list[TimeRule],
        obligations: list[dict[str, str]],
        plans: list[dict[str, object]],
    ) -> object | None:
        """Classify an action and collect scheduling hints; fails open on outage."""
        review = getattr(self.resolver, "review_action", None)
        if review is None:
            return None
        try:
            return await review(
                character,
                action,
                roster=roster,
                world_context=current_state,
                time_rules=time_rules,
                obligations=obligations,
                plans=plans,
            )
        except Exception:
            # A review outage must not block play; only a returned verdict rejects.
            LOGGER.exception("Action review failed room=%s", self.room_code)
            return None

    def _canonical_roster_name(self, name: object, roster: list[str]) -> str | None:
        """Map a model-supplied name to the canonical roster spelling."""
        if not isinstance(name, str):
            return None
        folded = name.strip().casefold()
        if not folded:
            return None
        return next((item for item in roster if item.casefold() == folded), None)

    def _drop_proposals_for(self, character: str | None) -> None:
        """Forget proposals/plans touching a character who left (disconnect = agreement)."""
        if not character:
            return
        folded = character.casefold()
        self.pending_proposals = [
            proposal
            for proposal in self.pending_proposals
            if str(proposal["from"]).casefold() != folded
            and str(proposal["to"]).casefold() != folded
        ]
        self.agreed_plans = [
            plan
            for plan in self.agreed_plans
            if all(str(name).casefold() != folded for name in plan["participants"])
        ]

    def _apply_action_contract_locked(
        self,
        character: str,
        action: str,
        roster: list[str],
        verdict: object | None,
    ) -> int | None:
        """Enforce proposal/response obligations and return a clamped duration."""
        open_obligations = [
            proposal
            for proposal in self.pending_proposals
            if proposal["to"] == character and not proposal["resolved"]
        ]
        response_to_raw = proposal_to_raw = matched_rule = plan_id = None
        move_to_raw = take_item_raw = referenced_item_raw = None
        instant_comm = True
        duration = None
        response = None
        if verdict is not None:
            response_to_raw = getattr(verdict, "response_to", None)
            proposal_to_raw = getattr(verdict, "proposal_to", None)
            instant_comm = bool(getattr(verdict, "instant_comm", True))
            duration = getattr(verdict, "duration_minutes", None)
            matched_rule = getattr(verdict, "matched_rule", None)
            response = getattr(verdict, "response", None)
            plan_id = getattr(verdict, "plan_id", None)
            move_to_raw = getattr(verdict, "move_to", None)
            take_item_raw = getattr(verdict, "take_item", None)
            referenced_item_raw = getattr(verdict, "referenced_item", None)
        response_to = self._canonical_roster_name(response_to_raw, roster)
        proposal_to = self._canonical_roster_name(proposal_to_raw, roster)
        if response_to_raw and response_to is None:
            raise ValueError("该提议的发起者不在队伍中。")
        if response_to:
            match = next((item for item in open_obligations if item["from"] == response_to), None)
            if match is None:
                raise ValueError("没有来自该角色的待回应提议。")
            # Answering consumes the obligation; agreement forms a shared plan.
            self.pending_proposals = [item for item in self.pending_proposals if item is not match]
            if str(response or "").casefold() == "agree":
                self.plan_counter += 1
                self.agreed_plans.append(
                    {
                        "id": f"plan-{self.plan_counter}",
                        "participants": [str(match["from"]), str(match["to"])],
                        "content": str(match["content"]),
                        "round": self.round_counter + 1,
                        "status": "agreed",
                        "executed": [],
                    }
                )
        elif open_obligations:
            raise ValueError(
                f"请先回应 {open_obligations[0]['from']} 的提议（同意 / 无视 / 拒绝）。"
            )
        if proposal_to_raw:
            if proposal_to is None:
                raise ValueError("提议的对象必须是队伍中的其他角色。")
            if proposal_to == character:
                raise ValueError("不能向自己提议。")
            if not instant_comm:
                raise ValueError("无法与目标即时沟通，请改为具体的行动（前往或传讯）。")
            if any(
                str(item["to"]).casefold() == proposal_to.casefold() and not item["resolved"]
                for item in self.pending_proposals
            ):
                raise ValueError(f"{proposal_to} 还有未回应的提议，请等待其先处理。")
            if len(self.pending_proposals) >= 50:
                raise ValueError("待回应的提议过多，请先处理已有提议。")
            self.pending_proposals.append(
                {
                    "from": character,
                    "to": proposal_to,
                    "content": action,
                    "resolved": False,
                    "response": None,
                    "round": self.round_counter + 1,
                }
            )
        if plan_id:
            plan = next(
                (item for item in self.agreed_plans if str(item["id"]) == str(plan_id)), None
            )
            if plan is None or character not in plan["participants"]:
                raise ValueError("没有可执行的约定。")
            if character not in plan["executed"]:
                plan["executed"].append(character)
            if all(name in plan["executed"] for name in plan["participants"]):
                plan["status"] = "done"
        if response_to_raw or proposal_to_raw:
            duration = 0
        move_to = take_item = investigate = None
        current_region = self.region_of.get(character, self.world_start_region)
        if self.regions:
            if move_to_raw:
                target = self._canonical_roster_name(move_to_raw, list(self.regions))
                if target is None:
                    raise ValueError("没有这个地区。")
                region = self.regions.get(current_region) if current_region else None
                link = (
                    next((item for item in region.links if item.to == target), None)
                    if region is not None
                    else None
                )
                if link is None:
                    raise ValueError("无法从这里直接前往该地区。")
                if link.requires_item:
                    holder = self.item_location.get(link.requires_item)
                    if holder != character:
                        item = self.items.get(link.requires_item)
                        name = item.name if item is not None else link.requires_item
                        raise ValueError(f"前往该地区需要携带：{name}。")
                move_to = target
            if take_item_raw:
                target = self._canonical_roster_name(take_item_raw, list(self.items))
                if target is None or self.item_location.get(target) != current_region:
                    raise ValueError("这件物品不在你所在的地区。")
                item = self.items.get(target)
                if item is not None and not item.carryable:
                    raise ValueError("这件物品无法携带。")
                take_item = target
            if referenced_item_raw:
                target = self._canonical_roster_name(referenced_item_raw, list(self.items))
                if target is not None and self.item_location.get(target) in (
                    current_region,
                    character,
                ):
                    investigate = target
        value = None if duration is None else max(0, int(duration))
        if value is not None and matched_rule:
            rule = next((item for item in self.time_rules if item.activity == matched_rule), None)
            if rule is not None:
                value = max(rule.minutes_min, min(rule.minutes_max, value))
        return {
            "duration": value,
            "move_to": move_to,
            "take_item": take_item,
            "investigate": investigate,
        }

    async def _skip_vote(self, client_id: str, data: dict[str, object]) -> None:
        """Record a skip vote; a unanimous vote skips the active player's input."""
        if self.schedule_mode == "strict":
            await self._skip_vote_scheduled(client_id, data)
            return
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
                raise ValueError("请先加入房间再投票。")
            if self.state is not GameState.ACTIVE_TURN:
                raise ValueError("只能在回合进行中投票。")
            target = self.active_player_id
            if target is None:
                raise ValueError("当前没有可跳过的回合。")
            if target == client_id:
                raise ValueError("你不能投票跳过自己。")
            target_player = self.players[target]
            eligible = {
                player_id
                for player_id, player in self.players.items()
                if player_id != target and player.is_connected and player.character_name is not None
            }
            if not eligible:
                raise ValueError("没有其他在线玩家可以投票。")
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
                            f"投票通过：{removed_target} 的本轮输入被跳过，"
                            "其玩家已被移出游戏，可以重新加入。"
                        )
                    },
                )
            )
        if directive is not None:
            await self.sender.broadcast_global(directive)

    async def _skip_vote_scheduled(self, client_id: str, data: dict[str, object]) -> None:
        """Unanimously remove a windowed idle player from a strict game."""
        target = data.get("target")
        if not isinstance(target, str) or not target:
            raise ValueError("请指定要跳过的玩家。")
        vote_event = None
        removed: tuple[str, str, str] | None = None
        window_events: list[ServerEvent] = []
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            voter = self.players.get(client_id)
            if voter is None or not voter.is_connected:
                raise ValueError("请先加入房间再投票。")
            if self.state is not GameState.ACTIVE_TURN:
                raise ValueError("只能在行动窗口内投票。")
            target_player = self.players.get(target)
            if target_player is None or target_player.character_name is None:
                raise ValueError("目标玩家不存在。")
            if target == client_id:
                raise ValueError("你不能投票跳过自己。")
            if target in self.inflight:
                raise ValueError("目标正在行动中，无法跳过。")
            if target not in self.window:
                raise ValueError("目标当前不在行动窗口。")
            eligible = {
                pid
                for pid, player in self.players.items()
                if pid != target and player.is_connected and player.character_name is not None
            }
            if not eligible:
                raise ValueError("没有其他在线玩家可以投票。")
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
            if len(votes) >= len(eligible):
                removed = (target, target_player.name, target_player.character_name)
                self.skip_votes.pop(target, None)
                self._drop_proposals_for(target_player.character_name)
                self.players.pop(target, None)
                if target in self.join_order:
                    self.join_order.remove(target)
                self.claims.pop(target_player.character_name, None)
                self.window.discard(target)
                self.holds.discard(target)
                self.turn_queue = deque(x for x in self.turn_queue if x != target)
                if not self.window:
                    self._advance_schedule_locked()
                if self.state is GameState.ACTIVE_TURN:
                    window_events = self._window_events_locked()
        if vote_event is not None:
            await self.sender.broadcast_global(vote_event)
        if removed is not None:
            _, _human, character = removed
            LOGGER.info("Skipped player removed room=%s player=%s", self.room_code, removed[0])
            await self.sender.send_personal(
                removed[0],
                ServerEvent(
                    type="removed",
                    payload={"msg": "你被全员投票跳过并移出了游戏，重新加入房间即可继续。"},
                ),
            )
            await self.sender.broadcast_global(
                ServerEvent(
                    type="system_msg", payload={"msg": f"投票通过：{character} 被移出游戏。"}
                )
            )
            await self.sender.broadcast_global(self._player_roster_event())
        for event in window_events:
            await self.sender.broadcast_global(event)

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
            self.round_durations.clear()
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
                "truth_context": self._truth_context(),
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
                raise ValueError("只有房主可以重试暂停的回合。")
            if self.inference_task is not None and not self.inference_task.done():
                raise ValueError("上一轮请求仍在处理中。")
            if self.schedule_mode == "strict":
                schedule = self.pending_schedule
                if not self.round_paused or schedule is None:
                    raise ValueError("没有可重试的暂停结算。")
                completed = tuple(schedule["completed"])
                next_end = int(schedule["next_end"])
                self._launch_job_locked(
                    lambda epoch: self._resolve_scheduled(epoch, completed, next_end),
                    GameState.AWAITING_LLM,
                )
            else:
                if not self.round_paused or self.pending_resolution is None:
                    raise ValueError("没有可重试的暂停回合。")
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
                    raise LLMResolutionError("骰点计划参与者无效。")
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
                event_context=self._combined_event_context(pending),
            )
        else:
            resolution = await self.resolver.generate_resolution(
                llm_actions,
                pending["dice"],
                hidden_rolls=pending["hidden"],
                current_time=self._authoritative_time_label(),
                event_context=self._combined_event_context(pending),
            )
        if (
            set(resolution.player_resolutions) != set(llm_actions)
            or not resolution.global_narrative.strip()
            or any(not text.strip() for text in resolution.player_resolutions.values())
        ):
            raise LLMResolutionError("结算参与者无效或全局叙述为空。")
        truth_findings = await self._collect_action_truth_answers(participants, actions)
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
                    self._drop_proposals_for(character)
                    LOGGER.info(
                        "Skipped player removed room=%s player=%s character=%s",
                        self.room_code,
                        item,
                        character,
                    )
                self.current_scenario_state = resolution.global_narrative
                # Event injections consumed by this round's request are spent.
                self.pending_event_injections = []
                auto_truths: list[tuple[str, str, str]] = []
                for question_id, character, text in truth_findings:
                    existing = self.truth_answers.get(question_id, [])
                    if any(
                        entry["character"] == character and entry["text"] == text
                        for entry in existing
                    ):
                        continue
                    entry = {"character": character, "text": text}
                    self.truth_answers.setdefault(question_id, []).append(entry)
                    auto_truths.append((question_id, character, text))
                    LOGGER.info(
                        "Action truth recorded room=%s question=%s character=%s",
                        self.room_code,
                        question_id,
                        character,
                    )
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
                self.round_durations.clear()
                self.previous_actions = previous_by_name
                self.turn_queue = deque(self.join_order)
                self._shuffle_turn_queue_locked()
                self.pending_resolution = None
                self.state = GameState.ACTIVE_TURN
                directive = self._next_turn_locked()
                truth_reveals = self._refresh_truth_reveals_locked()
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
                                "msg": ("你被全员投票跳过并移出了游戏，重新加入房间即可继续。")
                            },
                        ),
                    )
                await self.sender.broadcast_global(self._player_roster_event())
            if directive is not None:
                await self.sender.broadcast_global(
                    ServerEvent(type="round_start", payload={"round_number": number + 1})
                )
                await self.sender.broadcast_global(directive)
            await self._announce_truth_reveals(truth_reveals)
            for question_id, character, text in auto_truths:
                await self.sender.broadcast_global(
                    ServerEvent(
                        type="truth_answer",
                        payload={
                            "question_id": question_id,
                            "character": character,
                            "text": text,
                            "round": number,
                            "source": "action",
                        },
                    )
                )

    async def _request_history(self, client_id: str, data: dict[str, object]) -> None:
        """Serve an older page of the room's public round history."""
        before_raw = data.get("before_round")
        if before_raw is not None and (
            isinstance(before_raw, bool) or not isinstance(before_raw, int)
        ):
            raise ValueError("'before_round' 必须是整数。")
        async with self.lock:
            if not CURRENT_OWNER.get()():
                return
            player = self.players.get(client_id) or self.pending_players.get(client_id)
            if player is None or not player.is_connected:
                raise ValueError("请先加入房间再请求历史。")
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
    engine.schedule_mode = str(data.get("schedule_mode", "round"))
    engine.regions = {
        region.id: region
        for region in (
            Region(**entry) for entry in data.get("regions", []) if isinstance(entry, dict)
        )
    }
    engine.items = {
        item.id: item
        for item in (Item(**entry) for entry in data.get("items", []) if isinstance(entry, dict))
    }
    engine.world_start_region = str(data.get("world_start_region", ""))
    engine.region_of = {str(key): str(value) for key, value in data.get("region_of", {}).items()}
    engine.visited_regions = {str(value) for value in data.get("visited_regions", [])}
    engine.region_flags = {str(value) for value in data.get("region_flags", [])}
    engine.item_location = {
        str(key): str(value) for key, value in data.get("item_location", {}).items()
    }
    engine.investigated_items = {str(value) for value in data.get("investigated_items", [])}
    engine.region_state = {
        str(key): str(value) for key, value in data.get("region_state", {}).items()
    }
    engine._shuffle_turn_queue_locked()
    engine.host_client_id = data.get("host_client_id")
    engine.scenario_title = data.get("scenario_title")
    engine.original_scenario = data.get("original_scenario")
    engine.fixed_opening = str(data.get("fixed_opening", ""))
    engine.private_guidance = str(data.get("private_guidance", ""))
    engine.current_scenario_state = data.get("current_scenario_state")
    engine.opening_scenario = data.get("opening_scenario")
    if engine.fixed_opening:
        set_opening = getattr(engine.resolver, "set_opening", None)
        if set_opening is not None:
            set_opening(engine.fixed_opening)
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
    engine.plugins = [
        RoomPlugin(**plugin) for plugin in data.get("plugins", []) if isinstance(plugin, dict)
    ]
    engine.truth_revealed = {str(item) for item in data.get("truth_revealed", [])}
    engine.truth_answers = {
        str(question_id): [
            {"character": str(entry.get("character", "")), "text": str(entry.get("text", ""))}
            for entry in entries
            if isinstance(entry, dict)
        ]
        for question_id, entries in data.get("truth_answers", {}).items()
        if isinstance(entries, list)
    }
    engine.truth_used_round = {
        str(name): int(round_number)
        for name, round_number in data.get("truth_used_round", {}).items()
    }
    engine.pending_proposals = [
        dict(proposal)
        for proposal in data.get("pending_proposals", [])
        if isinstance(proposal, dict)
    ]
    engine.agreed_plans = [
        dict(plan) for plan in data.get("agreed_plans", []) if isinstance(plan, dict)
    ]
    engine.plan_counter = int(data.get("plan_counter", 0))
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

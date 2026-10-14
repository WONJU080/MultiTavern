"""Orchestration: one host client plus one AI seat per character.

The runner drives real WebSocket clients, so everything it exercises — room
creation, scenario injection, character claims, turn directives or strict-mode
action windows, the action referee and round resolution — is the same path a
human party takes. It supports both scheduling modes:

* ``round`` — one turn directive at a time; every seat answers in turn order.
* ``strict`` — every seat in the window answers at once, each action gets its
  own in-game duration, and the map/codex are fetched before deciding.

Seats never use ``hold``: a playtest exists to move the story, and holding
without anyone acting is exactly the livelock the watchdog below guards against.
"""

from __future__ import annotations

import asyncio
import logging
import time
import uuid
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Callable
from urllib.parse import urlsplit

from core.schemas import Character
from playtest import protocol as P
from playtest.agent import (
    SCRIPTED_ACTIONS,
    OpenAICompatBrain,
    ScriptedBrain,
    SeatAuthError,
    SeatBrain,
    SeatError,
    SeatTurn,
    TruthRequest,
    fallback_action,
    stable_offset,
)
from playtest.card import CardReport
from playtest.coverage import build_coverage, build_health
from playtest.config import PlaytestConfig, SeatPlan, plan_seats
from playtest.protocol import Event, ProtocolError
from playtest.report import RunReport, run_directory, slugify
from playtest.transport import Transport, TransportError, WebSocketTransport
from playtest.world import PublicView, is_actionable
from logic.plugins import truths_config

LOGGER = logging.getLogger(__name__)

HOST_NAME = "主持人"
SEAT_NAME_SUFFIX = "·AI"
TRANSPORT_ERROR_EVENT = "__transport_error"
# Wakes the play loop when a seat task decides the run must stop.
WAKE_EVENT = "__wake"

BOOTSTRAP_TIMEOUT_SECONDS = 60.0
JOIN_TIMEOUT_SECONDS = 30.0
START_TIMEOUT_SECONDS = 180.0
ACTION_SETTLE_SECONDS = 120.0
SNAPSHOT_TIMEOUT_SECONDS = 5.0
MAX_ATTEMPTS_PER_ROUND = 3
# Hard ceiling on submissions per seat per round: every rejection re-arms the
# seat, so this is what stops a driver-side hot loop for any rejection reason.
MAX_SUBMISSIONS_PER_ROUND = 6
MAX_REPORTED_WARNINGS = 200
SPIN_EVENT_BUDGET = 120
TIMING_REJECTION_MARKERS = (
    "不是你的行动窗口",
    "不是行动窗口",
    "行动仍在进行中",
    "请等待",
)
SHUTDOWN_GRACE_SECONDS = 3.0
ROUND_RETRY_DELAY_SECONDS = 1.0
MAX_REPORTED_NOTICES = 40
MAX_PROGRESS_EVENTS = 300

TransportFactory = Callable[[str], Transport]
BrainFactory = Callable[[SeatPlan, Character], SeatBrain]


class RunAborted(RuntimeError):
    """Raised when the run cannot continue and must be reported as stopped."""


@dataclass
class Connection:
    """One client connection with its own player-visible view."""

    client_id: str
    name: str
    transport: Transport
    view: PublicView
    reader: asyncio.Task[None] | None = None
    map_event: asyncio.Event | None = None
    codex_event: asyncio.Event | None = None
    truth_event: asyncio.Event | None = None


@dataclass
class SeatRuntime:
    """One AI player: its character, its brain and its call statistics."""

    character: Character
    style: str
    plan: SeatPlan
    brain: SeatBrain
    connection: Connection | None = None
    calls: int = 0
    failures: int = 0
    retries: int = 0
    fallbacks: int = 0
    forced_actions: int = 0
    truth_submissions: int = 0
    actions: list[str] = field(default_factory=list)
    latencies: list[float] = field(default_factory=list)
    submitted_rounds: set[int] = field(default_factory=set)
    attempt_round: int = 0
    round_attempts: int = 0
    submissions_round: int = 0
    submissions_round_number: int = 0
    sidelined_round: int = 0
    pending: bool = False
    truth_pending: bool = False

    def as_dict(self, *, dry_run: bool) -> dict[str, Any]:
        """Return the seat's report row."""
        seat_llm = self.plan.llm
        endpoint = urlsplit(seat_llm.endpoint).netloc if seat_llm.endpoint else ""
        average = sum(self.latencies) / len(self.latencies) if self.latencies else 0.0
        return {
            "character": self.character.name,
            "style": self.style,
            "model": "scripted" if dry_run else (seat_llm.model_name or ""),
            "endpoint": endpoint,
            "calls": self.calls,
            "failures": self.failures,
            "retries": self.retries,
            "fallbacks": self.fallbacks,
            "forced": self.forced_actions,
            "truth": self.truth_submissions,
            "actions": len(self.actions),
            "average_seconds": round(average, 2),
            "is_host_seat": self.plan.host_seat,
        }


def is_timing_rejection(message: str) -> bool:
    """Return whether a rejection is about timing rather than action content.

    The server rejects an action that arrives outside its window; retrying the
    model would only burn calls, so the seat waits for the next directive.
    """
    return any(marker in message for marker in TIMING_REJECTION_MARKERS)


def _shorten(text: str, limit: int = 80) -> str:
    """Collapse one action into a single readable progress line."""
    collapsed = " ".join(str(text).split())
    if len(collapsed) <= limit:
        return collapsed
    return collapsed[: limit - 1] + "…"


def websocket_transport_factory(config: PlaytestConfig) -> TransportFactory:
    """Return the default factory that talks to the configured server."""

    def factory(client_id: str) -> Transport:
        return WebSocketTransport.connect_to(
            config.server.url,
            client_id,
            verify_tls=config.server.verify_tls,
            use_proxy=config.server.proxy,
        )

    return factory


def default_brain_factory(config: PlaytestConfig) -> BrainFactory:
    """Return the default seat brain factory (scripted in dry-run mode)."""
    dry_run = config.run.dry_run

    def factory(plan: SeatPlan, character: Character) -> SeatBrain:
        if dry_run:
            return ScriptedBrain(
                start_index=stable_offset(f"{plan.character}|{plan.style}", len(SCRIPTED_ACTIONS)),
                action_limit=plan.llm.action_limit,
            )
        return OpenAICompatBrain(
            plan.llm,
            character,
            plan.style,
            timeout=config.run.seat_timeout_seconds,
            action_limit=plan.llm.action_limit,
        )

    return factory


def dm_credential(config: PlaytestConfig) -> dict[str, Any] | None:
    """Build the create_room ``llm`` section for the configured DM credential."""
    if config.dm.mode == "borrow":
        return {"mode": "borrow"}
    section: dict[str, Any] = {"mode": "own"}
    if config.dm.endpoint:
        section["endpoint"] = config.dm.endpoint
    if config.dm.provider:
        section["provider"] = config.dm.provider
    if config.dm.model_name:
        section["model_name"] = config.dm.model_name
    key = config.dm.resolved_api_key()
    if key:
        section["api_key"] = key
    return section


class PlaytestRunner:
    """Run one scenario card with a party of AI seats."""

    def __init__(
        self,
        config: PlaytestConfig,
        card: CardReport,
        *,
        transport_factory: TransportFactory | None = None,
        brain_factory: BrainFactory | None = None,
        clock: Callable[[], float] = time.monotonic,
        wall: Callable[[], datetime] = datetime.now,
        logger: logging.Logger = LOGGER,
        progress: Callable[[dict[str, Any]], None] | None = None,
    ) -> None:
        """Store the card, the config and the injectable collaborators."""
        if card.setup is None:
            raise ValueError("卡牌未通过校验，无法跑测。")
        self.config = config
        self.card = card
        self.setup = card.setup
        self.transport_factory = transport_factory or websocket_transport_factory(config)
        self.brain_factory = brain_factory or default_brain_factory(config)
        self.clock = clock
        self.wall = wall
        self.logger = logger
        self.progress = progress
        self.progress_events: list[dict[str, Any]] = []

        self.queue: asyncio.Queue[tuple[Connection, Event]] = asyncio.Queue()
        self.connections: list[Connection] = []
        self.host: Connection | None = None
        self.seats: list[SeatRuntime] = []
        self.seats_by_client: dict[str, SeatRuntime] = {}
        self.pending_actions: dict[str, asyncio.Future[tuple[str, str]]] = {}
        self.pending_truth: dict[str, asyncio.Future[tuple[str, str]]] = {}
        self.action_tasks: set[asyncio.Task[None]] = set()

        # Card-level capabilities drive which optional requests the seats make.
        self.has_map = bool(self.setup.regions)
        self.has_codex = bool(self.setup.items or self.setup.events)
        self.truth_enabled = truths_config(self.setup.plugins) is not None

        self.rounds: dict[int, dict[str, Any]] = {}
        self.token_usage: list[dict[str, Any]] = []
        self.errors: list[str] = []
        self.rejections: list[str] = []
        self.pauses: list[str] = []
        self.round_retries = 0
        self.warnings: list[str] = []
        self.notices: list[str] = []
        self.room_code = ""
        self.stop_reason = ""
        self.started_at = ""
        self.finished_at = ""
        self.started_monotonic = 0.0
        self.round_started: dict[int, float] = {}
        self.snapshot_round: dict[str, int] = {}
        self.reported_rounds: set[int] = set()
        self.inflight_actions: dict[str, str] = {}
        self.inflight_action_round: dict[str, int] = {}
        self.progress_counter = 0
        self._last_progress_counter = 0
        self._events_since_progress = 0
        self.dm_thinking = False
        self.ended = False
        self.shutting_down = False
        # Strict-mode liveness: a window that repeats without moving the clock
        # means nobody submitted a real action, so seats stop asking the model.
        self.current_round = 0
        self.window_time = ""
        self.last_progress_at = 0.0
        self.force_action = False
        self.livelock = False
        self.stalled = False
        self.host_lost = False
        self.forced_stop = False
        self.abort_code = ""
        self.cancelled = False
        self.partial_report: RunReport | None = None
        self.map_state: dict[str, Any] | None = None
        self.codex_state: dict[str, Any] | None = None
        self.revealed_truths: dict[str, dict[str, Any]] = {}
        self.truth_started: set[tuple[str, str]] = set()
        self.queued_truth: dict[str, list[tuple[str, str]]] = {}

    def _emit(self, kind: str, message: str, **extra: Any) -> None:
        """Publish one progress line for a UI or log, never raising."""
        event = {
            "kind": kind,
            "message": message,
            "elapsed": round(max(0.0, self.clock() - self.started_monotonic), 2),
            **extra,
        }
        self.progress_events.append(event)
        del self.progress_events[:-MAX_PROGRESS_EVENTS]
        if self.progress is None:
            return
        try:
            self.progress(event)
        except Exception:  # noqa: BLE001 - progress must never break a run
            self.logger.debug("progress callback failed", exc_info=True)

    # -- public entry point ---------------------------------------------
    async def run(self) -> RunReport:
        """Execute the run and always return a report."""
        self.started_at = self.wall().isoformat(timespec="seconds")
        self.started_monotonic = self.clock()
        self.run_dir = run_directory(self.config.report.out_dir, self.card.title, now=self.wall())
        try:
            await self._bootstrap()
            await self._play()
        except RunAborted as exc:
            self.stop_reason = self.stop_reason or str(exc)
        except (TransportError, ProtocolError) as exc:
            self.stop_reason = f"连接层错误：{exc}"
            self.errors.append(self.stop_reason)
        except asyncio.CancelledError:
            self.stop_reason = self.stop_reason or "运行被取消"
            self.cancelled = True
            raise
        finally:
            self.finished_at = self.finished_at or self.wall().isoformat(timespec="seconds")
            await self._shutdown()
            if self.cancelled:
                # A stopped run still has a story: keep what resolved.
                try:
                    self.partial_report = self._build_report()
                except Exception:  # noqa: BLE001 - never mask the cancellation
                    self.logger.debug("partial report failed", exc_info=True)
        self.finished_at = self.wall().isoformat(timespec="seconds")
        report = self._build_report()
        self._emit(
            "stopped",
            f"跑测结束：{report.stop_reason}",
            verdict=report.verdict,
            rounds=report.rounds_completed,
        )
        return report

    # -- connection plumbing --------------------------------------------
    async def _open_connection(self, name: str, *, is_host: bool) -> Connection:
        """Open one client connection and start its reader."""
        client_id = str(uuid.uuid4())
        transport = self.transport_factory(client_id)
        await transport.open()
        view = PublicView(client_id=client_id, name=name, is_host=is_host)
        connection = Connection(
            client_id=client_id,
            name=name,
            transport=transport,
            view=view,
            map_event=asyncio.Event(),
            codex_event=asyncio.Event(),
            truth_event=asyncio.Event(),
        )
        connection.reader = asyncio.create_task(self._pump(connection))
        self.connections.append(connection)
        return connection

    async def _pump(self, connection: Connection) -> None:
        """Read raw messages into the shared queue until the socket fails."""
        try:
            while True:
                raw = await connection.transport.receive()
                await self.queue.put((connection, Event.from_wire(raw)))
        except (TransportError, ProtocolError) as exc:
            await self.queue.put(
                (connection, Event(type=TRANSPORT_ERROR_EVENT, payload={"msg": str(exc)}))
            )

    async def _next(self, timeout: float) -> tuple[Connection, Event]:
        """Return the next event, or fail when nothing arrives in time."""
        try:
            return await asyncio.wait_for(self.queue.get(), timeout)
        except TimeoutError as exc:
            raise RunAborted(self._timeout_reason()) from exc

    def _timeout_reason(self) -> str:
        """Describe a silent-server timeout for the report."""
        if self.dm_thinking:
            return "DM 长时间没有结算（等待超时）"
        return "长时间没有收到任何事件（看门狗）"

    async def _expect(
        self,
        predicate: Callable[[Connection, Event], bool],
        *,
        timeout: float,
        description: str,
        connection: Connection | None = None,
    ) -> Event:
        """Consume events until one matches, aborting on a client error."""
        deadline = self.clock() + timeout
        while True:
            remaining = deadline - self.clock()
            if remaining <= 0:
                raise RunAborted(f"等待{description}超时。")
            current, event = await self._next(remaining)
            self._apply(current, event)
            if predicate(current, event):
                return event
            if connection is not None and current is not connection:
                continue
            if event.type in (P.ERROR, TRANSPORT_ERROR_EVENT):
                raise RunAborted(f"{description}失败：{event.text('msg')}")

    # -- bootstrap -------------------------------------------------------
    async def _bootstrap(self) -> None:
        """Create the room, inject the card, seat every character and start."""
        self.host = await self._open_connection(HOST_NAME, is_host=True)
        await self.host.transport.send(
            P.create_room(HOST_NAME, credential=dm_credential(self.config))
        )
        auth = await self._expect(
            lambda connection, event: event.type == P.AUTH_OK,
            timeout=BOOTSTRAP_TIMEOUT_SECONDS,
            description="创建房间",
            connection=self.host,
        )
        self.room_code = auth.text("invite_code")
        if not self.room_code:
            raise RunAborted("服务器没有返回邀请码。")
        self.logger.info("playtest room created code=%s", self.room_code)
        self._emit("phase", f"房间已创建（{self.room_code}）", room_code=self.room_code)

        payload = dict(self.card.payload)
        await self.host.transport.send(P.scenario_init(payload))
        await self._expect(
            lambda connection, event: event.type == P.SCENARIO_READY,
            timeout=START_TIMEOUT_SECONDS,
            description="生成剧本标题",
            connection=self.host,
        )

        self._emit("phase", "剧本已注入，等待座位入座")
        await self._join_seats()
        await self.host.transport.send(P.start_game())
        await self._expect(
            lambda connection, event: event.type == P.STATE_UPDATE,
            timeout=START_TIMEOUT_SECONDS,
            description="开局叙事",
            connection=self.host,
        )

    async def _join_seats(self) -> None:
        """Claim every cast member for one seat after the cast exists."""
        characters = {character.name: character for character in self.setup.cast}
        plans = plan_seats(
            self.config.seats,
            [character.name for character in self.setup.cast],
            host_character=self.setup.host_character or None,
        )
        for plan in plans:
            character = characters[plan.character]
            brain = self.brain_factory(plan, character)
            if self.config.run.debug_prompts and hasattr(brain, "response_sink"):
                brain.response_sink = self._response_sink(plan.character)
            if not callable(getattr(brain, "act", None)):
                raise RunAborted(
                    f"座位 {plan.character} 的大脑不可用：brain_factory 必须直接返回带 "
                    "act() 的对象（不要写成 async def）。"
                )
            if plan.host_seat:
                connection = self.host
                assert connection is not None
            else:
                connection = await self._open_connection(
                    f"{plan.character}{SEAT_NAME_SUFFIX}", is_host=False
                )
                await connection.transport.send(
                    P.join_room(connection.name, self.room_code, plan.character)
                )
                await self._expect(
                    lambda current, event: event.type == P.AUTH_OK,
                    timeout=JOIN_TIMEOUT_SECONDS,
                    description=f"座位 {plan.character} 入座",
                    connection=connection,
                )
            seat = SeatRuntime(
                character=character,
                style=plan.style,
                plan=plan,
                brain=brain,
                connection=connection,
            )
            self.seats.append(seat)
            self.seats_by_client[connection.client_id] = seat
            self.logger.info(
                "playtest seat ready character=%s style=%s", plan.character, plan.style
            )
            self._emit(
                "seat",
                f"座位就绪：{plan.character}（{plan.style}）",
                character=plan.character,
            )

    # -- play loop -------------------------------------------------------
    async def _play(self) -> None:
        """Consume events, answer turns and enforce the run budgets."""
        while True:
            reason = self._stop_reason()
            if reason:
                self.stop_reason = reason
                return
            timeout = self.config.run.stall_seconds * (2 if self.dm_thinking else 1)
            try:
                connection, event = await asyncio.wait_for(self.queue.get(), timeout)
            except TimeoutError:
                self.stalled = True
                self.stop_reason = self._timeout_reason()
                return
            self._apply(connection, event)
            self._react(connection, event)
            self._watch_for_spinning()

    def _watch_for_spinning(self) -> None:
        """Stop paying for a loop that neither resolves nor advances anything.

        A referee can keep rejecting every action; each rejection re-arms the
        seat, so without this guard the driver would call the model forever.
        """
        if self.progress_counter != self._last_progress_counter:
            self._last_progress_counter = self.progress_counter
            self._events_since_progress = 0
            return
        self._events_since_progress += 1
        if self.force_action or self.shutting_down:
            return
        idle_for = self.clock() - max(self.last_progress_at, self.started_monotonic)
        if (
            self._events_since_progress >= SPIN_EVENT_BUDGET
            or idle_for >= 2 * self.config.run.stall_seconds
        ):
            self.force_action = True
            self._warn("连续多次驳回后没有任何进展，已改为强制提交兜底行动，避免空转与白花 token。")

    def _stop_reason(self) -> str:
        """Return why the run should stop, or an empty string."""
        if self.forced_stop:
            return self.stop_reason or "已停止"
        if self.ended:
            return self.stop_reason or "游戏已结束"
        if len(self._completed_rounds()) >= self.config.run.max_rounds:
            return f"达到回合上限（{self.config.run.max_rounds} 回合）"
        limit_minutes = self.config.run.max_minutes
        if self.clock() - self.started_monotonic >= limit_minutes * 60:
            return f"达到时间预算（{limit_minutes:.0f} 分钟）"
        budget = self.config.run.max_total_tokens
        if budget and (self._game_tokens() or 0) > budget:
            return f"达到 token 预算（{budget}）"
        return ""

    def _completed_rounds(self) -> list[int]:
        """Return the round numbers that produced a narrative."""
        return [number for number, record in self.rounds.items() if record.get("narrative")]

    def _game_tokens(self) -> int | None:
        """Return the latest cumulative game token count."""
        for snapshot in reversed(self.token_usage):
            game = snapshot.get("game")
            if isinstance(game, dict) and isinstance(game.get("total_tokens"), int):
                return int(game["total_tokens"])
        return None

    # -- event handling --------------------------------------------------
    def _apply(self, connection: Connection, event: Event) -> None:
        """Fold one event into views, bookkeeping, statistics and futures."""
        connection.view.apply(event)
        handler = {
            P.ACTION_ECHO: self._note_action_echo,
            P.STATE_UPDATE: self._note_state_update,
            P.TURN_WINDOW: self._note_window,
            P.TOKEN_USAGE: self._note_token_usage,
            P.DM_THINKING: self._note_dm_thinking,
            P.SYSTEM_MSG: self._note_system_message,
            P.ERROR: self._note_error,
            P.GAME_ENDED: self._note_ended,
            P.ROOM_CLOSED: self._note_ended,
            P.REMOVED: self._note_removed,
            P.MAP_STATE: self._note_snapshot,
            P.CODEX_STATE: self._note_snapshot,
            P.TRUTH_STATE: self._note_snapshot,
            P.TRUTH_RESULT: self._note_truth_result,
            P.TRUTH_QUESTION: self._note_truth_question,
            P.TRUTH_ANSWER: self._note_truth_answer,
            TRANSPORT_ERROR_EVENT: self._note_transport_error,
        }.get(event.type)
        if handler is not None:
            handler(connection, event)

    def _round_record(self, number: int) -> dict[str, Any]:
        """Return the mutable report record for one round."""
        return self.rounds.setdefault(
            number,
            {
                "number": number,
                "game_time": "",
                "actions": {},
                "narrative": "",
                "outcomes": {},
                "dice": {},
                "seconds": None,
            },
        )

    def _note_action_echo(self, connection: Connection, event: Event) -> None:
        number = event.number("round_number") or connection.view.round_number
        actor = event.text("player_name")
        action = event.text("action")
        if actor and action:
            # In strict mode one submission can resolve at a later boundary than
            # the one it was echoed for, so hold it until its round commits; the
            # hint is only a fallback for an action that never resolves.
            self.inflight_actions[actor] = action
            self.inflight_action_round[actor] = number
        # Strict mode broadcasts every submission: only this seat's own echo
        # settles its pending action, or another player's echo would accept it.
        if actor and actor == connection.view.character:
            self.last_progress_at = self.clock()
            self.progress_counter += 1
            self._resolve_action(connection, ("accepted", ""))

    def _note_state_update(self, connection: Connection, event: Event) -> None:
        number = event.number("round_number")
        if not number:
            return
        self.round_retries = 0  # the DM is healthy again
        self.last_progress_at = self.clock()
        record = self._round_record(number)
        record["narrative"] = event.text("global_narrative")
        record["game_time"] = event.text("game_time")
        record["outcomes"] = {
            str(key): str(value) for key, value in event.mapping("player_resolutions").items()
        }
        record["dice"] = {
            str(key): int(value)
            for key, value in event.mapping("dice_results").items()
            if isinstance(value, int) and not isinstance(value, bool)
        }
        for actor, action in event.mapping("submitted_actions").items():
            record["actions"].setdefault(str(actor), str(action))
        resolved = set(record["outcomes"]) | set(event.mapping("player_resolutions"))
        for actor in list(self.inflight_actions):
            if actor in resolved:
                record["actions"].setdefault(actor, self.inflight_actions.pop(actor))
                self.inflight_action_round.pop(actor, None)
        started = self.round_started.get(number)
        if started is not None and record["seconds"] is None:
            record["seconds"] = round(self.clock() - started, 2)
        next_round = number + 1
        if next_round not in self.round_started:
            self.round_started[next_round] = self.clock()
        # One state_update is broadcast to every connection; report each round once.
        if number not in self.reported_rounds:
            self.reported_rounds.add(number)
            self._emit(
                "round",
                f"第 {number} 回合结算完成"
                + (f"（{record['game_time']}）" if record.get("game_time") else ""),
                round=number,
            )
            for actor, action in record["actions"].items():
                self._emit("round-action", f"  · {actor}：{_shorten(action)}", round=number)

    def _note_token_usage(self, connection: Connection, event: Event) -> None:
        del connection
        self.token_usage.append(dict(event.payload))

    def _note_dm_thinking(self, connection: Connection, event: Event) -> None:
        del connection
        self.dm_thinking = bool(event.payload.get("active"))

    def _note_system_message(self, connection: Connection, event: Event) -> None:
        del connection
        message = event.text("msg")
        if message and message not in self.notices:
            self.notices.append(message)
            del self.notices[:-MAX_REPORTED_NOTICES]

    def _note_error(self, connection: Connection, event: Event) -> None:
        """Route a server error: a refused submission is retried, not a failure."""
        message = event.text("msg") or "未知错误"
        if event.payload.get("round_paused") is True:
            # Broadcast to everyone; only the host acts on it (and only once).
            if connection is self.host:
                self._retry_paused_round(message)
            return
        seat = self.seats_by_client.get(connection.client_id)
        waiting_truth = bool(seat is not None and seat.truth_pending and not seat.pending)
        waiting_action = connection.client_id in self.pending_actions
        if waiting_truth:
            self._resolve_truth(connection, ("rejected", message))
        elif waiting_action:
            self._resolve_action(connection, ("rejected", message))
        who = connection.view.character or connection.name
        if waiting_truth or waiting_action:
            # The driver answers with a retry or a fallback; that is normal play.
            if message not in self.rejections:
                self.rejections.append(message)
                del self.rejections[:-MAX_REPORTED_WARNINGS]
            self._emit("rejected", f"{who} 的提交被驳回：{message}")
            return
        if message in self.errors:
            return  # the same broadcast reaches every connection; record it once
        self.errors.append(message)
        self._emit("error", f"{who}：{message}")

    def _note_snapshot(self, connection: Connection, event: Event) -> None:
        """Wake a seat waiting for the map, the codex or its truth state."""
        if event.type == P.MAP_STATE:
            self.map_state = dict(event.payload)
        elif event.type == P.CODEX_STATE:
            self.codex_state = dict(event.payload)
        waiter = {
            P.MAP_STATE: connection.map_event,
            P.CODEX_STATE: connection.codex_event,
            P.TRUTH_STATE: connection.truth_event,
        }.get(event.type)
        if waiter is not None:
            waiter.set()

    def _note_truth_question(self, connection: Connection, event: Event) -> None:
        """Record a revealed question for coverage reporting."""
        del connection
        question_id = event.text("id")
        if not question_id:
            return
        self.revealed_truths.setdefault(
            question_id,
            {"id": question_id, "text": event.text("text"), "answers": []},
        )

    def _note_truth_answer(self, connection: Connection, event: Event) -> None:
        """Record an accepted claim for coverage reporting."""
        del connection
        question_id = event.text("question_id")
        if not question_id:
            return
        entry = self.revealed_truths.setdefault(
            question_id, {"id": question_id, "text": "", "answers": []}
        )
        claim = {"character": event.text("character"), "text": event.text("text")}
        if claim not in entry["answers"]:
            entry["answers"].append(claim)

    def _note_truth_result(self, connection: Connection, event: Event) -> None:
        valid = event.payload.get("valid")
        reason = event.text("reason")
        if valid is True:
            self._resolve_truth(connection, ("accepted", ""))
        else:
            self._resolve_truth(connection, ("rejected", reason or "真相陈述被驳回。"))

    def _note_window(self, connection: Connection, event: Event) -> None:
        """Track whether strict-mode windows are moving the in-game clock."""
        del connection
        self.current_round = event.number("round_number") or self.current_round
        game_time = event.text("game_time")
        if game_time and game_time != self.window_time:
            self.window_time = game_time
            self.last_progress_at = self.clock()
            self.force_action = False
            return
        if self.force_action:
            return
        idle_for = self.clock() - max(self.last_progress_at, self.started_monotonic)
        if idle_for >= self.config.run.stall_seconds:
            self.force_action = True
            self.livelock = True
            self._warn("行动窗口在游戏内时间上原地循环，已改为强制座位提交行动以避免空转。")

    def _note_ended(self, connection: Connection, event: Event) -> None:
        del connection
        self.ended = True
        message = event.text("msg") or "游戏已结束。"
        if not self.stop_reason:
            self.stop_reason = message
        elif message not in self.notices:
            self.notices.append(message)
            del self.notices[:-MAX_REPORTED_NOTICES]

    def _note_removed(self, connection: Connection, event: Event) -> None:
        del connection
        self._warn(event.text("msg") or "有玩家被移出游戏。")

    def _note_transport_error(self, connection: Connection, event: Event) -> None:
        message = event.text("msg") or "连接中断。"
        self._resolve_action(connection, ("timeout", message))
        if self.shutting_down:
            # Closing the room closes every socket; that is not a failure.
            self.logger.debug("transport closed during shutdown: %s", message)
            return
        self._warn(f"{connection.name}：{message}")
        if connection is self.host and not self.stop_reason:
            self.ended = True
            self.host_lost = True
            self.stop_reason = f"主机连接中断：{message}"

    def _react(self, connection: Connection, event: Event) -> None:
        """Start a seat turn when the server hands this connection the turn."""
        if event.type == P.TRUTH_QUESTION:
            self._maybe_start_truth(connection, event)
            return
        if not is_actionable(event, connection.client_id):
            return
        if self._stop_reason():
            # A budget or ending already fired: do not open another turn.
            return
        seat = self.seats_by_client.get(connection.client_id)
        if seat is None or seat.pending:
            return
        number = event.number("round_number") or connection.view.round_number + 1
        if seat.sidelined_round == number or number in seat.submitted_rounds:
            return
        self.round_started.setdefault(number, self.clock())
        seat.pending = True
        task = asyncio.create_task(self._seat_turn(seat, number))
        self.action_tasks.add(task)
        task.add_done_callback(self.action_tasks.discard)

    def _maybe_start_truth(self, connection: Connection, event: Event) -> None:
        """Queue a newly revealed possible-truth question for this seat."""
        if not self.truth_enabled or self.shutting_down:
            return
        seat = self.seats_by_client.get(connection.client_id)
        if seat is None:
            return
        question_id = event.text("id")
        if not question_id or (connection.client_id, question_id) in self.truth_started:
            return
        self.truth_started.add((connection.client_id, question_id))
        self.queued_truth.setdefault(connection.client_id, []).append(
            (question_id, event.text("text"))
        )
        self._drain_truth_queue(seat)

    def _drain_truth_queue(self, seat: SeatRuntime) -> None:
        """Start the next queued truth answer once the seat is free."""
        connection = seat.connection
        assert connection is not None
        queue = self.queued_truth.get(connection.client_id) or []
        if not queue or seat.pending or seat.truth_pending or self.shutting_down:
            return
        question_id, question_text = queue.pop(0)
        seat.truth_pending = True
        task = asyncio.create_task(self._truth_turn(seat, question_id, question_text))
        self.action_tasks.add(task)
        task.add_done_callback(self.action_tasks.discard)

    async def _refresh_snapshots(self, seat: SeatRuntime) -> None:
        """Fetch the map and codex once per strict-mode window before deciding."""
        connection = seat.connection
        assert connection is not None
        if self.setup.schedule != "strict":
            return
        if self.snapshot_round.get(connection.client_id) == self.current_round:
            return
        self.snapshot_round[connection.client_id] = self.current_round
        if self.has_map and connection.map_event is not None:
            await self._request_snapshot(connection, P.map_request(), connection.map_event)
        if self.has_codex and connection.codex_event is not None:
            await self._request_snapshot(connection, P.codex_request(), connection.codex_event)

    async def _request_snapshot(
        self, connection: Connection, request: dict[str, Any], waiter: asyncio.Event
    ) -> None:
        """Send one snapshot request and wait briefly for its answer."""
        waiter.clear()
        try:
            await connection.transport.send(request)
            await asyncio.wait_for(waiter.wait(), SNAPSHOT_TIMEOUT_SECONDS)
        except (TransportError, TimeoutError):
            self.logger.debug("snapshot request timed out for %s", connection.name)

    # -- acting ----------------------------------------------------------
    def _arm_future(
        self, registry: dict[str, asyncio.Future[tuple[str, str]]], client_id: str
    ) -> asyncio.Future[tuple[str, str]]:
        """Register a submission future resolved by its echo, result or error."""
        future: asyncio.Future[tuple[str, str]] = asyncio.get_running_loop().create_future()
        previous = registry.get(client_id)
        if previous is not None and not previous.done():
            previous.cancel()
        registry[client_id] = future
        return future

    def _settle_future(
        self,
        registry: dict[str, asyncio.Future[tuple[str, str]]],
        connection: Connection,
        result: tuple[str, str],
    ) -> None:
        """Resolve a pending submission future, if any."""
        future = registry.get(connection.client_id)
        if future is not None and not future.done():
            future.set_result(result)

    def _arm_action(self, client_id: str) -> asyncio.Future[tuple[str, str]]:
        """Register the future resolved by the next action echo or error."""
        return self._arm_future(self.pending_actions, client_id)

    def _resolve_action(self, connection: Connection, result: tuple[str, str]) -> None:
        """Resolve the pending action of one connection, if any."""
        self._settle_future(self.pending_actions, connection, result)

    def _arm_truth(self, client_id: str) -> asyncio.Future[tuple[str, str]]:
        """Register the future resolved by the next truth result or error."""
        return self._arm_future(self.pending_truth, client_id)

    def _resolve_truth(self, connection: Connection, result: tuple[str, str]) -> None:
        """Resolve the pending truth submission of one connection, if any."""
        self._settle_future(self.pending_truth, connection, result)

    def _submission_allowed(self, seat: SeatRuntime, round_number: int) -> bool:
        """Bound how often one seat may submit within a single round."""
        if seat.submissions_round_number != round_number:
            seat.submissions_round_number = round_number
            seat.submissions_round = 0
        if seat.submissions_round >= MAX_SUBMISSIONS_PER_ROUND:
            if seat.sidelined_round != round_number:
                seat.sidelined_round = round_number
                self._warn(
                    f"{seat.character.name}：本回合已提交 {seat.submissions_round} 次仍未成功，"
                    "停止为该座位提交（等下一回合）。"
                )
            return False
        seat.submissions_round += 1
        return True

    async def _submit(self, seat: SeatRuntime, action: str, round_number: int) -> tuple[str, str]:
        """Send one action and wait for the server to accept or reject it."""
        assert seat.connection is not None
        if not self._submission_allowed(seat, round_number):
            return ("sidelined", "")
        future = self._arm_action(seat.connection.client_id)
        try:
            await seat.connection.transport.send(P.action(action))
        except TransportError as exc:
            return ("timeout", str(exc))
        try:
            return await asyncio.wait_for(future, ACTION_SETTLE_SECONDS)
        except TimeoutError:
            return ("timeout", "")
        finally:
            if self.pending_actions.get(seat.connection.client_id) is future:
                self.pending_actions.pop(seat.connection.client_id, None)

    async def _truth_turn(self, seat: SeatRuntime, question_id: str, question_text: str) -> None:
        """Ask one seat for a truth claim and submit it, best effort."""
        connection = seat.connection
        assert connection is not None
        brain_answer = getattr(seat.brain, "answer_truth", None)
        try:
            if brain_answer is None:
                return
            view = connection.view
            if connection.truth_event is not None:
                await self._request_snapshot(connection, P.truth_request(), connection.truth_event)
            if view.truth_used_round is not None and view.truth_used_round >= self.current_round:
                return
            request = TruthRequest(
                character=seat.character.name,
                style=seat.style,
                question_id=question_id,
                question_text=question_text,
                digest=view.digest(),
                round_number=self.current_round,
                max_chars=view.truth_max_chars,
            )
            try:
                async with asyncio.timeout(self.config.run.seat_timeout_seconds):
                    answer = await brain_answer(request)
            except SeatError as exc:
                self.warnings.append(f"{seat.character.name}：{exc}")
                return
            except TimeoutError:
                self.warnings.append(
                    f"{seat.character.name}：真相陈述超过 "
                    f"{self.config.run.seat_timeout_seconds:.0f}s 未返回。"
                )
                return
            if not answer:
                return
            future = self._arm_truth(connection.client_id)
            await connection.transport.send(P.truth_submit(question_id, answer))
            try:
                status, reason = await asyncio.wait_for(future, ACTION_SETTLE_SECONDS)
            except TimeoutError:
                status, reason = "timeout", ""
            finally:
                if self.pending_truth.get(connection.client_id) is future:
                    self.pending_truth.pop(connection.client_id, None)
            if status == "accepted":
                seat.truth_submissions += 1
            else:
                self.warnings.append(
                    f"{seat.character.name}：真相陈述未被接受"
                    + (f"（{reason}）" if reason else "。")
                )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a truth turn must not kill the run
            self.errors.append(f"{seat.character.name}：真相流程异常 {type(exc).__name__}: {exc}")
        finally:
            seat.truth_pending = False

    async def _seat_turn(self, seat: SeatRuntime, round_number: int) -> None:
        """Ask one seat for an action within a bounded per-round budget.

        A referee that keeps rejecting an action must not turn into a hot loop:
        the engine re-sends a directive or window after every rejection, so an
        unbounded driver would call the model and the server forever without
        resolving anything.
        """
        assert seat.connection is not None
        try:
            if seat.sidelined_round == round_number:
                return
            if seat.attempt_round != round_number:
                seat.attempt_round = round_number
                seat.round_attempts = 0
            await self._refresh_snapshots(seat)
            if self.force_action:
                # No progress for a while: submit a deterministic action instead
                # of spending another model call.
                forced = fallback_action(seat.character, round_number, 1)
                if await self._submit(seat, forced, round_number) == ("accepted", ""):
                    self._accept_action(seat, round_number, forced, forced=True)
                return
            rejection = ""
            while seat.round_attempts < MAX_ATTEMPTS_PER_ROUND:
                if self.forced_stop:
                    # A fatal condition (e.g. a rejected key) ends the attempt loop.
                    return
                seat.round_attempts += 1
                action = await self._brain_action(seat, round_number, rejection)
                if not action:
                    seat.failures += 1
                    rejection = ""
                    continue
                status, message = await self._submit(seat, action, round_number)
                if status == "accepted":
                    self._accept_action(seat, round_number, action)
                    return
                if status == "sidelined":
                    return
                seat.failures += 1
                if status == "rejected" and is_timing_rejection(message):
                    # The window moved on: this is not the model's fault, so the
                    # attempt does not count. Wait for the next directive instead.
                    seat.round_attempts -= 1
                    self._warn(f"{seat.character.name}：{message}")
                    return
                if status == "rejected":
                    seat.retries += 1
                    rejection = message or "行动被拒绝。"
                    continue
                rejection = ""
            self._warn(
                f"{seat.character.name}：连续 {MAX_ATTEMPTS_PER_ROUND} 次提交都被驳回，改用兜底行动。"
            )
            for attempt in range(1, MAX_ATTEMPTS_PER_ROUND + 1):
                if self.forced_stop:
                    return
                fallback = fallback_action(seat.character, round_number, attempt)
                seat.fallbacks += 1
                status, message = await self._submit(seat, fallback, round_number)
                if status == "accepted":
                    self._accept_action(seat, round_number, fallback, forced=True)
                    return
                if status == "sidelined":
                    return
                self._warn(
                    f"{seat.character.name}：第 {attempt} 种兜底行动也被驳回"
                    + (f"（{message}）" if message else "。")
                )
            seat.sidelined_round = round_number
            self._warn(
                f"{seat.character.name}：本回合所有兜底行动都被驳回，已停止为该座位提交（等下一回合）。"
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 - a seat must not kill the run
            self.errors.append(f"{seat.character.name}：座位异常 {type(exc).__name__}: {exc}")
        finally:
            seat.pending = False
            self._drain_truth_queue(seat)
            self._resume_after_turn(seat, round_number)

    def _response_sink(self, character: str) -> Callable[[str], None]:
        """Return a sink that appends one seat's raw replies to the run directory."""
        path = self.run_dir / "responses" / f"{slugify(character, fallback='seat')}.md"

        def sink(text: str) -> None:
            try:
                path.parent.mkdir(parents=True, exist_ok=True)
                with path.open("a", encoding="utf-8") as handle:
                    handle.write(text)
            except OSError as exc:
                self.logger.debug("response dump failed: %s", exc)

        return sink

    def _dump_prompt(self, seat: SeatRuntime, turn: SeatTurn) -> None:
        """Record the exact text a seat was asked with, for card debugging."""
        if not self.config.run.debug_prompts:
            return
        from playtest.agent import build_turn_prompt

        directory = self.run_dir / "prompts"
        try:
            directory.mkdir(parents=True, exist_ok=True)
            name = slugify(seat.character.name, fallback="seat")
            with (directory / f"{name}.md").open("a", encoding="utf-8") as handle:
                system = (
                    getattr(seat.brain, "system_prompt", "") or "（脚本化座位，没有系统提示词）"
                )
                handle.write(f"\n\n## 第 {turn.round_number} 回合\n\n### system\n\n{system}\n")
                handle.write(f"\n### user\n\n{build_turn_prompt(turn)}\n")
                if turn.rejection:
                    handle.write(f"\n（本次带驳回理由重试：{turn.rejection}）\n")
        except OSError as exc:
            self.logger.debug("prompt dump failed: %s", exc)

    async def _brain_action(self, seat: SeatRuntime, round_number: int, rejection: str) -> str:
        """Ask one seat's model for an action, turning failures into warnings."""
        assert seat.connection is not None
        seat.calls += 1
        started = self.clock()
        turn = SeatTurn(
            character=seat.character.name,
            style=seat.style,
            digest=seat.connection.view.digest(),
            round_number=round_number,
            action_limit=seat.plan.llm.action_limit,
            rejection=rejection,
        )
        self._dump_prompt(seat, turn)
        action = ""
        try:
            async with asyncio.timeout(self.config.run.seat_timeout_seconds):
                action = await seat.brain.act(turn)
        except SeatAuthError as exc:
            # A rejected key cannot be retried away: stop before spending more.
            self._abort(f"座位凭据被拒绝：{exc}", code="seat_credentials")
        except SeatError as exc:
            self._warn(f"{seat.character.name}：{exc}")
        except TimeoutError:
            self._warn(
                f"{seat.character.name}：座位调用超过 "
                f"{self.config.run.seat_timeout_seconds:.0f}s 未返回。"
            )
        seat.latencies.append(max(0.0, self.clock() - started))
        return action

    def _accept_action(
        self, seat: SeatRuntime, round_number: int, action: str, *, forced: bool = False
    ) -> None:
        """Record one accepted action and report it to the UI."""
        seat.actions.append(action)
        seat.submitted_rounds.add(round_number)
        if forced:
            seat.forced_actions += 1
        self.inflight_actions[seat.character.name] = action
        self.last_progress_at = self.clock()
        self.progress_counter += 1
        self._emit(
            "action",
            f"{seat.character.name}：{_shorten(action)}",
            character=seat.character.name,
            round=round_number,
        )

    def _resume_after_turn(self, seat: SeatRuntime, round_number: int) -> None:
        """Re-arm a seat whose window arrived while it was busy."""
        connection = seat.connection
        if connection is None or self.shutting_down:
            return
        task = asyncio.current_task()
        if task is not None and task.cancelling():
            # This turn is being torn down (stop/shutdown): do not spawn new work.
            return
        if round_number in seat.submitted_rounds or seat.sidelined_round == round_number:
            return
        if not connection.view.is_my_turn:
            return
        self._react(
            connection,
            Event(
                type=(
                    P.TURN_WINDOW if connection.view.schedule_mode == "strict" else P.TURN_DIRECTIVE
                ),
                payload={
                    "active_player_ids": list(connection.view.active_player_ids),
                    "active_player_id": connection.client_id,
                    "round_number": round_number,
                },
            ),
        )

    def _abort(self, reason: str, *, code: str = "aborted") -> None:
        """Stop the run: retrying cannot fix this condition."""
        if not self.stop_reason:
            self.stop_reason = reason
            self.abort_code = code
            self.errors.append(reason)
            self._emit("error", reason)
        self.forced_stop = True
        self._wake()

    def _retry_paused_round(self, message: str) -> None:
        """Ask the host to redo a failed resolution, or stop after the budget."""
        if self.shutting_down or self.forced_stop:
            return
        limit = self.config.run.max_round_retries
        if self.round_retries >= limit:
            self._abort(f"DM 结算连续 {limit} 次失败，已停止（{message}）", code="dm_round_failed")
            return
        self.round_retries += 1
        if message not in self.pauses:
            self.pauses.append(message)
        self._emit(
            "warning",
            f"DM 结算失败，请房主重试本回合（第 {self.round_retries}/{limit} 次）：{message}",
        )
        task = asyncio.create_task(self._send_round_retry())
        self.action_tasks.add(task)
        task.add_done_callback(self.action_tasks.discard)

    async def _send_round_retry(self) -> None:
        """Send retry_round as the host, after giving the engine a moment."""
        if self.host is None:
            return
        try:
            await asyncio.sleep(ROUND_RETRY_DELAY_SECONDS)
            await self.host.transport.send(P.retry_round())
        except TransportError as exc:
            self._warn(f"重试本回合失败：{exc}")
        except asyncio.CancelledError:
            raise

    def _wake(self) -> None:
        """Nudge the play loop so a stop decided in a seat task takes effect."""
        connection = self.host or (self.connections[0] if self.connections else None)
        if connection is None:
            return
        self.queue.put_nowait((connection, Event(type=WAKE_EVENT, payload={})))

    def _warn(self, message: str) -> None:
        """Record one warning, dropping exact duplicates."""
        if message in self.warnings:
            return
        self.warnings.append(message)
        del self.warnings[:-MAX_REPORTED_WARNINGS]
        self._emit("warning", message)

    # -- shutdown --------------------------------------------------------
    async def _shutdown(self) -> None:
        """End or close the room, then close every connection."""
        self.shutting_down = True
        for task in list(self.action_tasks):
            task.cancel()
        if self.action_tasks:
            await asyncio.gather(*self.action_tasks, return_exceptions=True)
        if self.host is not None and self.host.transport.connected:
            await self._close_room()
        for seat in self.seats:
            closer = getattr(seat.brain, "aclose", None)
            if closer is None:
                continue
            try:
                await closer()
            except Exception:  # noqa: BLE001 - closing must not mask the outcome
                self.logger.debug("closing seat brain failed", exc_info=True)
        for connection in self.connections:
            if connection.reader is not None:
                connection.reader.cancel()
        await asyncio.gather(
            *(connection.transport.close() for connection in self.connections),
            return_exceptions=True,
        )

    async def _close_room(self) -> None:
        """Best-effort end_game then close_room so no room is left behind."""
        assert self.host is not None
        try:
            if not self.ended:
                await self.host.transport.send(P.end_game())
                await self._drain(SHUTDOWN_GRACE_SECONDS, stop_when=lambda: self.ended)
            if not self.ended:
                await self.host.transport.send(P.close_room())
                await self._drain(SHUTDOWN_GRACE_SECONDS, stop_when=lambda: self.ended)
        except (TransportError, ProtocolError) as exc:
            self.warnings.append(f"收尾时房间未能正常关闭：{exc}")

    async def _drain(self, seconds: float, *, stop_when: Callable[[], bool] | None = None) -> None:
        """Consume events briefly so final events reach the report."""
        deadline = self.clock() + seconds
        while True:
            if stop_when is not None and stop_when():
                return
            remaining = deadline - self.clock()
            if remaining <= 0:
                return
            try:
                connection, event = await asyncio.wait_for(self.queue.get(), remaining)
            except TimeoutError:
                return
            self._apply(connection, event)

    # -- report ----------------------------------------------------------
    def _build_report(self) -> RunReport:
        """Assemble the run report from views, statistics and configuration."""
        for actor, action in self.inflight_actions.items():
            hint = self.inflight_action_round.get(actor) or max(self.rounds, default=0)
            self._round_record(hint)["actions"].setdefault(actor, action)
        self.inflight_actions.clear()
        rounds = [self.rounds[number] for number in sorted(self.rounds)]
        seats = [seat.as_dict(dry_run=self.config.run.dry_run) for seat in self.seats]
        coverage = build_coverage(
            setup=self.setup,
            rounds=rounds,
            map_state=self.map_state,
            codex_state=self.codex_state,
            revealed_truths=list(self.revealed_truths.values()),
        )
        health = build_health(
            rounds=rounds,
            seats=seats,
            warnings=self.warnings,
            errors=self.errors,
            stop_reason=self.stop_reason or "未知",
            rejections=self.rejections,
            pauses=self.pauses,
            coverage=coverage,
            stalled=self.stalled,
            livelock=self.livelock,
            host_lost=self.host_lost,
            token_usage=self.token_usage,
        )
        return RunReport(
            card_path=str(self.card.path),
            card_digest=self.card.digest,
            card_title=self.card.title,
            schedule=self.setup.schedule,
            server_url=self.config.server.url,
            dm_mode=self.config.dm.mode,
            room_code=self.room_code,
            started_at=self.started_at,
            finished_at=self.finished_at,
            elapsed_seconds=round(max(0.0, self.clock() - self.started_monotonic), 2),
            stop_reason=self.stop_reason or "未知",
            dry_run=self.config.run.dry_run,
            rounds=rounds,
            seats=seats,
            token_usage=self.token_usage,
            errors=self.errors,
            rejections=self.rejections,
            pauses=self.pauses,
            warnings=self.warnings,
            notices=self.notices,
            coverage=coverage,
            health=health,
        )

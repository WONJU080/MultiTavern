"""End-to-end round-mode runs against the real ASGI gateway, with zero tokens.

The driver talks to ``create_app(FakeResolver)`` over the actual
``/ws/{client_id}`` endpoint. Only the DM's inference is faked and the seats are
scripted, so these tests exercise room creation, scenario injection, claims,
turn directives, the action referee and round resolution without a network.
"""

from __future__ import annotations

import asyncio
import json
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from playtest.agent import SeatError
from playtest.card import inspect_card
from playtest.config import PlaytestConfig
from playtest.runner import PlaytestRunner, dm_credential
from playtest.transport import TransportError
from logic.rooms import normalize_invite_code
from test_engine import FakeResolver

RUN_TIMEOUT_SECONDS = 60.0


@dataclass(frozen=True, slots=True)
class _Closed:
    """Marker pushed by a reader thread when the session ends."""

    message: str


class InProcessTransport:
    """Bridges one blocking TestClient websocket session into async code."""

    def __init__(self, session: Any, loop: asyncio.AbstractEventLoop) -> None:
        """Start a daemon reader thread that feeds an asyncio queue."""
        self.session = session
        self._loop = loop
        self._queue: asyncio.Queue[Any] = asyncio.Queue()
        self._closed = False
        self._thread = threading.Thread(target=self._read, daemon=True)
        self._thread.start()

    @property
    def connected(self) -> bool:
        """Return whether this transport is still usable."""
        return not self._closed

    def _read(self) -> None:
        """Forward blocking receives into the event loop until the session ends."""
        while True:
            try:
                message = self.session.receive_json()
            except Exception as exc:  # noqa: BLE001 - session teardown is expected
                self._push(_Closed(str(exc) or "会话已关闭"))
                return
            self._push(message)

    def _push(self, item: Any) -> None:
        """Hand one item to the loop, tolerating an already-closed loop."""
        try:
            self._loop.call_soon_threadsafe(self._queue.put_nowait, item)
        except RuntimeError:
            return

    async def open(self) -> None:
        """The test session is already connected when it is handed over."""
        return None

    async def send(self, envelope: dict[str, Any]) -> None:
        """Send one envelope through the blocking session."""
        await asyncio.to_thread(self.session.send_json, envelope)

    async def receive(self) -> dict[str, Any]:
        """Return the next server message, or fail when the session ended."""
        item = await self._queue.get()
        if isinstance(item, _Closed):
            raise TransportError(f"会话已结束：{item.message}")
        return item

    async def close(self) -> None:
        """Mark this transport closed; the hub closes the session itself."""
        self._closed = True


class InProcessHub:
    """Creates one in-process websocket session per client id."""

    def __init__(self, client: TestClient) -> None:
        """Store the test client whose lifespan hosts the server."""
        self.client = client
        self.sessions: list[Any] = []

    def transport_factory(self, client_id: str) -> InProcessTransport:
        """Open a session for a client id."""
        session = self.client.websocket_connect(f"/ws/{client_id}")
        session.__enter__()
        self.sessions.append(session)
        return InProcessTransport(session, asyncio.get_running_loop())

    def close(self) -> None:
        """Exit every session, ignoring teardown noise."""
        for session in reversed(self.sessions):
            try:
                session.__exit__(None, None, None)
            except Exception:  # noqa: BLE001 - teardown must not mask results
                pass
        self.sessions.clear()


def round_card(**overrides: Any) -> dict[str, Any]:
    """Return a small valid round-mode card (no regions, no clock)."""
    payload: dict[str, Any] = {
        "scenario": "【剧本：跑测卡】\n\n一扇锁着的门，两个人被留在门厅。",
        "guidance": "真相：钥匙藏在花盆底下，谁先找到谁占上风。",
        "characters": [
            {"name": "甲", "description": "谨慎的锁匠", "personality": "话少，先看后动"},
            {"name": "乙", "description": "急躁的记者", "personality": "抢话，爱冒险"},
        ],
    }
    payload.update(overrides)
    return payload


def write_card(tmp_path: Path, payload: Any, name: str = "card.anyworld.json") -> Path:
    """Write a card file into the test's temporary directory."""
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def build_config(**overrides: Any) -> PlaytestConfig:
    """Return a dry-run configuration with tight budgets."""
    raw: dict[str, Any] = {
        "server": {"url": "https://127.0.0.1:4141"},
        "run": {
            "max_rounds": 2,
            "max_minutes": 5,
            "stall_seconds": 10,
            "seat_timeout_seconds": 5,
            "dry_run": True,
        },
    }
    for key, value in overrides.items():
        if isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key] = {**raw[key], **value}
        else:
            raw[key] = value
    return PlaytestConfig.model_validate(raw)


def play(
    tmp_path: Path,
    payload: Any,
    *,
    config: PlaytestConfig | None = None,
    brain_factory: Any = None,
    resolver_factory: Any = FakeResolver,
) -> tuple[Any, dict[str, Any]]:
    """Run one game in-process and return the report plus the surviving rooms."""
    card = inspect_card(write_card(tmp_path, payload))
    assert card.ok, card.findings
    app = create_app(resolver_factory)
    with TestClient(app) as client:
        hub = InProcessHub(client)
        runner = PlaytestRunner(
            config or build_config(),
            card,
            transport_factory=hub.transport_factory,
            brain_factory=brain_factory,
        )
        try:
            report = asyncio.run(asyncio.wait_for(runner.run(), RUN_TIMEOUT_SECONDS))
        finally:
            hub.close()
        rooms = dict(client.app.state.registry.rooms)
    return report, rooms


def test_a_dry_run_plays_rounds_and_stops_at_the_cap(tmp_path: Path) -> None:
    """Scripted seats play real rounds until the round budget stops the run."""
    report, rooms = play(tmp_path, round_card())

    assert report.rounds_completed == 2
    assert "回合上限" in report.stop_reason
    assert report.room_code
    assert report.errors == []
    assert report.warnings == []
    assert report.card_title == "跑测卡"
    assert report.schedule == "round"

    for record in report.rounds:
        assert set(record["actions"]) == {"甲", "乙"}
        assert record["narrative"]
        assert record["seconds"] is not None

    assert len(report.seats) == 2
    for seat in report.seats:
        assert seat["model"] == "scripted"
        assert seat["calls"] >= 1
        assert seat["actions"] >= 1
        assert seat["failures"] == 0
        assert seat["is_host_seat"] is False

    # The room is closed (or handed to the sweeper) once the run is over.
    assert normalize_invite_code(report.room_code) not in rooms
    # And the report is JSON-serializable for downstream tooling.
    json.dumps(report.as_dict(), ensure_ascii=False)


def test_the_host_can_drive_one_of_the_characters(tmp_path: Path) -> None:
    """A card that claims host_character gives that seat the host connection."""
    report, _ = play(
        tmp_path, round_card(host_character="甲"), config=build_config(run={"max_rounds": 1})
    )

    assert report.rounds_completed == 1
    host_seats = [seat for seat in report.seats if seat["is_host_seat"]]
    assert [seat["character"] for seat in host_seats] == ["甲"]
    assert set(report.rounds[0]["actions"]) == {"甲", "乙"}


class _FailingBrain:
    """A seat whose model always fails."""

    def __init__(self) -> None:
        """Record how often the seat was asked."""
        self.calls = 0

    async def act(self, turn: Any) -> str:
        """Fail every call."""
        del turn
        self.calls += 1
        raise SeatError("模拟的模型故障")


class _SlowBrain:
    """A seat whose model never answers in time."""

    async def act(self, turn: Any) -> str:
        """Hang until the runner's timeout cancels the call."""
        del turn
        await asyncio.sleep(30)
        return "永远不会到达的行动"


def test_a_failing_seat_falls_back_and_the_round_still_resolves(tmp_path: Path) -> None:
    """One broken seat must not stall the whole table."""
    report, _ = play(
        tmp_path,
        round_card(),
        config=build_config(run={"max_rounds": 1}),
        brain_factory=lambda plan, character: _FailingBrain(),
    )

    assert report.rounds_completed == 1
    assert any("兜底行动" in item for item in report.warnings)
    assert all(seat["fallbacks"] == 1 for seat in report.seats)
    assert all(seat["failures"] >= 1 for seat in report.seats)
    assert set(report.rounds[0]["actions"]) == {"甲", "乙"}


def test_a_slow_seat_is_cut_off_by_the_seat_timeout(tmp_path: Path) -> None:
    """A hung seat call is cancelled and replaced by the fallback action."""
    report, _ = play(
        tmp_path,
        round_card(),
        config=build_config(run={"max_rounds": 1, "seat_timeout_seconds": 0.2}),
        brain_factory=lambda plan, character: _SlowBrain(),
    )

    assert report.rounds_completed == 1
    assert any("座位调用超过" in item for item in report.warnings)
    assert all(seat["fallbacks"] == 1 for seat in report.seats)


class UsageResolver(FakeResolver):
    """A fake DM that also reports token usage, so that path is covered."""

    def __init__(self) -> None:
        """Report one fixed usage snapshot per round."""
        super().__init__()
        self.last_token_usage = 4096
        self.rounds_begun: list[int] = []

    def begin_round_usage(self, number: int) -> None:
        """Record the round whose usage is being measured."""
        self.rounds_begun.append(number)

    def finish_round_usage(self, error: str | None = None) -> None:
        """Accept the engine's timing hook."""
        del error

    def usage_snapshot(self) -> dict[str, Any]:
        """Return prompt-free round and game totals."""
        return {
            "round": {"total_tokens": 1200, "input_tokens": 900, "completion_tokens": 300},
            "game": {"total_tokens": 4096},
            "round_failures": 0,
        }


def test_token_usage_events_reach_the_report(tmp_path: Path) -> None:
    """DM-side token totals are recorded so a run can be costed."""
    report, _ = play(
        tmp_path,
        round_card(),
        config=build_config(run={"max_rounds": 1}),
        resolver_factory=UsageResolver,
    )

    assert report.rounds_completed == 1
    assert report.token_usage
    assert report.final_game_tokens == 4096
    assert "4096" in report.summary_line()
    assert "DM 累计 token：4096" in report.to_markdown()


def test_an_invalid_card_is_refused_before_any_connection(tmp_path: Path) -> None:
    """The runner refuses to start without a validated card."""
    card = inspect_card(write_card(tmp_path, {"prompt_blocks": []}))

    with pytest.raises(ValueError):
        PlaytestRunner(build_config(), card)


def test_dm_credentials_never_leak_keys(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    """borrow maps to a mode flag; own carries the endpoint and key only."""
    monkeypatch.setenv("PLAYTEST_DM_KEY", "secret-value")

    borrow = dm_credential(build_config())
    assert borrow == {"mode": "borrow"}

    own = dm_credential(
        build_config(
            dm={
                "mode": "own",
                "endpoint": "https://api.example.com/v1",
                "api_key_env": "PLAYTEST_DM_KEY",
                "model_name": "m",
            }
        )
    )
    assert own["mode"] == "own"
    assert own["endpoint"] == "https://api.example.com/v1"
    assert own["model_name"] == "m"
    assert own["api_key"] == "secret-value"

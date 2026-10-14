"""Local web UI for the playtest driver.

A single-user tool: it serves one page on loopback, accepts the same
configuration the YAML file uses (including API keys, which stay in this
process's memory), and reports progress plus the finished report.

Nothing here touches the game server beyond what ``run`` already does; the page
only exists so a card author does not have to hand-edit YAML to spend tokens.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Awaitable, Callable, Sequence

from fastapi import FastAPI, HTTPException
from fastapi.responses import HTMLResponse, PlainTextResponse
from pydantic import BaseModel, ConfigDict, ValidationError

from playtest.card import inspect_card
from playtest.config import PlaytestConfig, format_validation_error, plan_seats
from playtest.doctor import Probe, run_checks
from playtest.report import write_report
from playtest.runner import BrainFactory, PlaytestRunner, TransportFactory

WEB_DIR = Path(__file__).resolve().parent / "web"
INDEX_FILE = WEB_DIR / "index.html"
DEFAULT_CARDS_DIRS = ("examples",)
MAX_RUNS_RETAINED = 20


class StartRunRequest(BaseModel):
    """One run request from the page: the card path plus a playtest config."""

    model_config = ConfigDict(extra="forbid")

    card: str
    config: dict[str, Any]


class CheckRequest(BaseModel):
    """One pre-flight request from the page."""

    model_config = ConfigDict(extra="forbid")

    card: str
    config: dict[str, Any]


@dataclass
class RunState:
    """One in-flight or finished run."""

    id: str
    card_path: str
    status: str = "running"
    started_at: float = field(default_factory=time.time)
    finished_at: float = 0.0
    progress: list[dict[str, Any]] = field(default_factory=list)
    report: dict[str, Any] | None = None
    markdown: str | None = None
    report_paths: list[str] = field(default_factory=list)
    error: str = ""
    runner: PlaytestRunner | None = None
    task: asyncio.Task[Any] | None = None

    def push(self, event: dict[str, Any]) -> None:
        """Record one progress event from the runner."""
        self.progress.append(event)
        del self.progress[:-500]

    def as_dict(self) -> dict[str, Any]:
        """Return the polling payload for the page."""
        return {
            "id": self.id,
            "card_path": self.card_path,
            "status": self.status,
            "elapsed": round((self.finished_at or time.time()) - self.started_at, 2),
            "progress": self.progress[-200:],
            "report": self.report,
            "report_paths": self.report_paths,
            "error": self.error,
        }


def card_summary(path: Path) -> dict[str, Any]:
    """Return the card row the page lists."""
    report = inspect_card(path)
    setup = report.setup
    return {
        "path": str(path),
        "title": report.title,
        "ok": report.ok,
        "schedule": report.schedule,
        "characters": [character.name for character in setup.cast] if setup else [],
        "counts": report.counts(),
        "errors": [item.message for item in report.errors],
        "warnings": [item.message for item in report.warnings],
    }


def _build_config(raw: dict[str, Any]) -> PlaytestConfig:
    """Validate a page-submitted configuration."""
    try:
        return PlaytestConfig.model_validate(raw)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=format_validation_error(exc)) from exc


def create_playtest_app(
    *,
    cards_dirs: Sequence[str] = DEFAULT_CARDS_DIRS,
    transport_factory: TransportFactory | None = None,
    brain_factory: BrainFactory | None = None,
    probe: Probe | None = None,
) -> FastAPI:
    """Build the local playtest UI and its JSON API."""
    application = FastAPI(title="Anyworld playtest")
    runs: dict[str, RunState] = {}
    order: list[str] = []

    def find_cards() -> list[Path]:
        """Return every card file under the configured directories."""
        found: list[Path] = []
        for directory in cards_dirs:
            base = Path(directory)
            if base.is_dir():
                found.extend(sorted(base.glob("*.anyworld.json")))
            elif base.is_file():
                found.append(base)
        return found

    async def execute(state: RunState, config: PlaytestConfig, card_path: str) -> None:
        """Run one playtest and keep its outcome on the state."""
        card = inspect_card(card_path)
        if not card.ok:
            state.status = "failed"
            state.finished_at = time.time()
            state.error = "卡牌未通过校验：" + "；".join(item.message for item in card.errors)
            return
        runner = PlaytestRunner(
            config,
            card,
            transport_factory=transport_factory,
            brain_factory=brain_factory,
            progress=state.push,
        )
        state.runner = runner
        try:
            report = await runner.run()
        except asyncio.CancelledError:
            state.status = "stopped"
            state.finished_at = time.time()
            state.error = "已手动停止。"
            partial = runner.partial_report
            if partial is not None:
                # Keep the rounds that did resolve so the report can be inspected.
                try:
                    written = write_report(
                        partial,
                        config.report.out_dir,
                        config.report.formats,
                        directory=runner.run_dir,
                    )
                    state.report_paths = [str(path) for path in written]
                except OSError as exc:
                    state.error = f"已手动停止；部分报告写入失败：{exc}"
                state.report = partial.as_dict()
                state.markdown = partial.to_markdown()
            raise
        except Exception as exc:  # noqa: BLE001 - the page must show the failure
            state.status = "failed"
            state.finished_at = time.time()
            state.error = f"{type(exc).__name__}: {exc}"
            return
        try:
            written = write_report(report, config.report.out_dir, config.report.formats)
            state.report_paths = [str(path) for path in written]
        except OSError as exc:
            state.error = f"报告写入失败：{exc}"
        state.report = report.as_dict()
        state.markdown = report.to_markdown()
        state.status = "finished"
        state.finished_at = time.time()

    @application.get("/", response_class=HTMLResponse)
    async def index() -> HTMLResponse:
        """Serve the single-page UI."""
        if not INDEX_FILE.exists():  # pragma: no cover - packaging guard
            raise HTTPException(status_code=500, detail="页面文件缺失。")
        return HTMLResponse(INDEX_FILE.read_text(encoding="utf-8"))

    @application.get("/api/cards")
    async def list_cards() -> dict[str, Any]:
        """List the cards the page can pick from."""
        return {
            "cards": [card_summary(path) for path in find_cards()],
            "cards_dirs": [str(item) for item in cards_dirs],
        }

    @application.post("/api/check")
    async def check(request: CheckRequest) -> dict[str, Any]:
        """Run the pre-flight checks without spending tokens."""
        config = _build_config(request.config)
        card = inspect_card(request.card)
        results = await run_checks(config, card, probe=probe)
        return {
            "checks": [item.as_dict() for item in results],
            "ok": all(not item.failed for item in results),
        }

    @application.post("/api/seat-plan")
    async def seat_plan(request: CheckRequest) -> dict[str, Any]:
        """Preview how the page's configuration maps onto the card's cast."""
        config = _build_config(request.config)
        card = inspect_card(request.card)
        if card.setup is None:
            return {"seats": []}
        plans = plan_seats(
            config.seats,
            [character.name for character in card.setup.cast],
            host_character=card.setup.host_character or None,
        )
        return {
            "seats": [
                {
                    "character": plan.character,
                    "style": plan.style,
                    "model": plan.llm.model_name or "",
                    "endpoint": plan.llm.endpoint or "",
                    "host_seat": plan.host_seat,
                }
                for plan in plans
            ]
        }

    @application.post("/api/runs")
    async def start_run(request: StartRunRequest) -> dict[str, Any]:
        """Start one playtest in the background."""
        config = _build_config(request.config)
        card = inspect_card(request.card)
        if not card.ok:
            raise HTTPException(
                status_code=400,
                detail="卡牌未通过校验：" + "；".join(item.message for item in card.errors),
            )
        state = RunState(id=uuid.uuid4().hex[:12], card_path=request.card)
        state.push(
            {
                "kind": "phase",
                "message": f"开始跑测《{card.title}》（{card.schedule} 模式）",
                "elapsed": 0.0,
            }
        )
        runs[state.id] = state
        order.append(state.id)
        while len(order) > MAX_RUNS_RETAINED:
            runs.pop(order.pop(0), None)
        state.task = asyncio.create_task(execute(state, config, request.card))
        return {"id": state.id}

    @application.get("/api/runs/{run_id}")
    async def get_run(run_id: str) -> dict[str, Any]:
        """Return the progress and report of one run."""
        state = runs.get(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail="没有这个跑测记录。")
        return state.as_dict()

    @application.get("/api/runs/{run_id}/report.md", response_class=PlainTextResponse)
    async def get_report_markdown(run_id: str) -> PlainTextResponse:
        """Return the finished run's Markdown report."""
        state = runs.get(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail="没有这个跑测记录。")
        if state.markdown is None:
            raise HTTPException(status_code=409, detail="跑测尚未结束。")
        return PlainTextResponse(state.markdown)

    @application.post("/api/runs/{run_id}/stop")
    async def stop_run(run_id: str) -> dict[str, Any]:
        """Cancel a running playtest."""
        state = runs.get(run_id)
        if state is None:
            raise HTTPException(status_code=404, detail="没有这个跑测记录。")
        if state.task is not None and not state.task.done():
            state.task.cancel()
        return {"status": state.status}

    return application


ProgressHook = Callable[[dict[str, Any]], None]


def serve(
    *,
    host: str = "127.0.0.1",
    port: int = 8787,
    cards_dirs: Sequence[str] = DEFAULT_CARDS_DIRS,
) -> Awaitable[None]:
    """Return the coroutine that serves the UI (used by the CLI)."""
    import uvicorn

    application = create_playtest_app(cards_dirs=cards_dirs)
    config = uvicorn.Config(application, host=host, port=port, log_level="warning")
    return uvicorn.Server(config).serve()

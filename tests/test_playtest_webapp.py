"""The local playtest UI: card listing, pre-flight checks and a run over the API."""

from __future__ import annotations

import asyncio
import json
import time
from pathlib import Path
from typing import Any

import pytest
from fastapi.testclient import TestClient

from api.server import create_app
from playtest.webapp import create_playtest_app
from test_engine import FakeResolver
from test_playtest_runner import InProcessHub, round_card, write_card


def build_app(tmp_path: Path, **kwargs: Any):
    """Build the UI app with a temporary card directory and report directory."""
    cards_dir = tmp_path / "cards"
    cards_dir.mkdir(exist_ok=True)
    options: dict[str, Any] = {
        "cards_dirs": [str(cards_dir)],
    }
    options.update(kwargs)
    return cards_dir, create_playtest_app(**options)


def dry_run_config(tmp_path: Path, card: Path, **run_overrides: Any) -> dict[str, Any]:
    """Return a page-shaped configuration for a scripted run."""
    limits = {
        "max_rounds": 2,
        "max_minutes": 5,
        "stall_seconds": 10,
        "seat_timeout_seconds": 5,
        "dry_run": True,
    }
    limits.update(run_overrides)
    return {
        "server": {"url": "https://127.0.0.1:4141", "verify_tls": False},
        "dm": {"mode": "borrow"},
        "seats": {"default": {"style": "rotate"}},
        "run": limits,
        "report": {"out_dir": str(tmp_path / "reports"), "formats": ["json", "markdown"]},
    }


def test_the_page_is_served(tmp_path: Path) -> None:
    """GET / returns the single-page UI."""
    _, app = build_app(tmp_path)
    with TestClient(app) as client:
        response = client.get("/")

    assert response.status_code == 200
    assert "跑测台" in response.text
    assert "/api/cards" in response.text


def test_card_listing_reports_playability(tmp_path: Path) -> None:
    """The page lists playable and unplayable cards with their reason."""
    cards_dir, app = build_app(tmp_path)
    write_card(cards_dir, round_card(), "good.anyworld.json")
    (cards_dir / "preset.anyworld.json").write_text(
        json.dumps({"prompt_blocks": []}), encoding="utf-8"
    )

    with TestClient(app) as client:
        payload = client.get("/api/cards").json()

    cards = {Path(item["path"]).name: item for item in payload["cards"]}
    assert cards["good.anyworld.json"]["ok"] is True
    assert cards["good.anyworld.json"]["schedule"] == "round"
    assert cards["good.anyworld.json"]["characters"] == ["甲", "乙"]
    assert cards["preset.anyworld.json"]["ok"] is False
    assert cards["preset.anyworld.json"]["errors"]


def test_check_endpoint_uses_the_injected_probe(tmp_path: Path) -> None:
    """A pre-flight request never touches the network in tests."""
    cards_dir, app = build_app(tmp_path)
    card = write_card(cards_dir, round_card())

    async def probe(
        url: str, api_key: str | None, verify_tls: bool, use_proxy: bool
    ) -> tuple[int, str]:
        del api_key, verify_tls, use_proxy
        return (200, "200") if url.startswith("https://127.0.0.1:4141/") else (0, "no route")

    _, app = build_app(tmp_path, probe=probe, cards_dirs=[str(cards_dir)])
    body = {"card": str(card), "config": dry_run_config(tmp_path, card)}

    with TestClient(app) as client:
        payload = client.post("/api/check", json=body).json()

    assert payload["ok"] is True
    levels = {item["name"]: item["level"] for item in payload["checks"]}
    assert levels["服务器连通"] == "ok"


def test_seat_plan_matches_the_card_cast(tmp_path: Path) -> None:
    """The page can preview which model each character will use."""
    cards_dir, app = build_app(tmp_path)
    card = write_card(cards_dir, round_card())
    config = dry_run_config(tmp_path, card)
    config["seats"] = {
        "default": {"endpoint": "https://x/v1", "model_name": "base", "style": "rotate"},
        "overrides": {"乙": {"model_name": "strong"}},
    }

    with TestClient(app) as client:
        payload = client.post("/api/seat-plan", json={"card": str(card), "config": config}).json()

    seats = {item["character"]: item for item in payload["seats"]}
    assert seats["甲"]["model"] == "base"
    assert seats["甲"]["style"] == "explore"
    assert seats["乙"]["model"] == "strong"


def test_an_invalid_configuration_is_rejected(tmp_path: Path) -> None:
    """Unknown keys in the submitted configuration fail with a 400."""
    cards_dir, app = build_app(tmp_path)
    card = write_card(cards_dir, round_card())
    config = dry_run_config(tmp_path, card)
    config["seats"] = {"defualt": {}}

    with TestClient(app) as client:
        response = client.post("/api/runs", json={"card": str(card), "config": config})

    assert response.status_code == 400
    assert "seats.defualt" in response.json()["detail"]


def test_an_unplayable_card_cannot_start_a_run(tmp_path: Path) -> None:
    """The API refuses a card the server would reject."""
    cards_dir, app = build_app(tmp_path)
    card = cards_dir / "preset.anyworld.json"
    card.write_text(json.dumps({"prompt_blocks": []}), encoding="utf-8")

    with TestClient(app) as client:
        response = client.post(
            "/api/runs", json={"card": str(card), "config": dry_run_config(tmp_path, card)}
        )

    assert response.status_code == 400
    assert "未通过校验" in response.json()["detail"]


def test_unknown_runs_are_not_found(tmp_path: Path) -> None:
    """Polling an unknown run id returns 404."""
    _, app = build_app(tmp_path)
    with TestClient(app) as client:
        assert client.get("/api/runs/nope").status_code == 404
        assert client.post("/api/runs/nope/stop").status_code == 404
        assert client.get("/api/runs/nope/report.md").status_code == 404


def test_a_dry_run_completes_through_the_api(tmp_path: Path) -> None:
    """The page can start a real run and read back its report."""
    game_app = create_app(FakeResolver)
    with TestClient(game_app) as game_client:
        hub = InProcessHub(game_client)
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        card = write_card(cards_dir, round_card())
        app = create_playtest_app(
            cards_dirs=[str(cards_dir)], transport_factory=hub.transport_factory
        )
        body = {"card": str(card), "config": dry_run_config(tmp_path, card)}
        try:
            with TestClient(app) as client:
                started = client.post("/api/runs", json=body)
                assert started.status_code == 200, started.text
                run_id = started.json()["id"]

                state = wait_for_run(client, run_id)

                assert state["status"] == "finished", state
                report = state["report"]
                assert report["run"]["rounds_completed"] == 2
                assert "回合上限" in report["run"]["stop_reason"]
                assert report["health"]["verdict"] == "unfinished"
                assert len(report["seats"]) == 2
                assert state["report_paths"], "报告应当写盘"
                for path in state["report_paths"]:
                    assert Path(path).exists()

                markdown = client.get(f"/api/runs/{run_id}/report.md")
                assert markdown.status_code == 200
                assert "跑测报告" in markdown.text

                progress = state["progress"]
                assert any(item["kind"] == "phase" for item in progress)
                rounds = [item for item in progress if item["kind"] == "round"]
                # One broadcast reaches every connection; each round is reported once.
                assert len(rounds) == 2, progress
                assert any(item["kind"] == "action" for item in progress)
                assert progress[-1]["kind"] == "stopped"
        finally:
            hub.close()


def wait_for_run(
    client: TestClient, run_id: str, *, timeout: float = 60.0, interval: float = 0.2
) -> dict[str, Any]:
    """Poll a run until it leaves the running state."""
    deadline = time.monotonic() + timeout
    state: dict[str, Any] = {}
    while time.monotonic() < deadline:
        state = client.get(f"/api/runs/{run_id}").json()
        if state["status"] != "running":
            return state
        time.sleep(interval)
    raise AssertionError(f"run did not finish in time: {state}")


def test_stopping_a_run_marks_it_stopped(tmp_path: Path) -> None:
    """A stopped run reports the interruption instead of a report."""
    game_app = create_app(FakeResolver)
    with TestClient(game_app) as game_client:
        hub = InProcessHub(game_client)
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        card = write_card(cards_dir, round_card())
        app = create_playtest_app(
            cards_dirs=[str(cards_dir)], transport_factory=hub.transport_factory
        )

        class Slow:
            """A brain that never answers in time."""

            async def act(self, turn: Any) -> str:
                del turn
                await asyncio.sleep(30)
                return "永远不会到达"

        def slow_brain(plan: Any, character: Any) -> Any:
            del plan, character
            return Slow()

        app = create_playtest_app(
            cards_dirs=[str(cards_dir)],
            transport_factory=hub.transport_factory,
            brain_factory=slow_brain,
        )
        body = {
            "card": str(card),
            "config": dry_run_config(tmp_path, card, dry_run=False, stall_seconds=60),
        }
        try:
            with TestClient(app) as client:
                run_id = client.post("/api/runs", json=body).json()["id"]
                time.sleep(0.5)
                stopped = client.post(f"/api/runs/{run_id}/stop")
                assert stopped.status_code == 200
                state = wait_for_run(client, run_id)
                assert state["status"] == "stopped"
                # Whatever resolved before the stop is still reported.
                if state["report"] is not None:
                    assert state["report_paths"]
                    assert (tmp_path / "reports").exists()
        finally:
            hub.close()


def test_a_stopped_run_writes_a_partial_report(tmp_path: Path) -> None:
    """Stopping mid-run must not throw away the rounds that already resolved."""
    game_app = create_app(FakeResolver)
    with TestClient(game_app) as game_client:
        hub = InProcessHub(game_client)
        cards_dir = tmp_path / "cards"
        cards_dir.mkdir()
        card = write_card(cards_dir, round_card())

        class Slow:
            """A seat that is slow enough to be interrupted mid-round."""

            async def act(self, turn: Any) -> str:
                del turn
                await asyncio.sleep(0.4)
                return "我先仔细查看四周。"

        def factory(plan: Any, character: Any) -> Any:
            del plan, character
            return Slow()

        app = create_playtest_app(
            cards_dirs=[str(cards_dir)],
            transport_factory=hub.transport_factory,
            brain_factory=factory,
        )
        config = dry_run_config(tmp_path, card, dry_run=False, max_rounds=4, stall_seconds=60)
        try:
            with TestClient(app) as client:
                run_id = client.post(
                    "/api/runs", json={"card": str(card), "config": config}
                ).json()["id"]
                time.sleep(1.2)  # let the first round resolve
                client.post(f"/api/runs/{run_id}/stop")
                state = wait_for_run(client, run_id)

                assert state["status"] == "stopped"
                assert state["report"] is not None, state
                assert state["report_paths"], state
                for path in state["report_paths"]:
                    assert Path(path).exists()
                markdown = client.get(f"/api/runs/{run_id}/report.md")
                assert markdown.status_code == 200
                assert "跑测报告" in markdown.text
        finally:
            hub.close()


def test_a_card_without_characters_is_listed_as_unplayable(tmp_path: Path) -> None:
    """Cards missing their cast never appear as runnable."""
    cards_dir, app = build_app(tmp_path)
    (cards_dir / "half.anyworld.json").write_text(
        json.dumps({"scenario": "只有一个剧本。"}, ensure_ascii=False), encoding="utf-8"
    )

    with TestClient(app) as client:
        cards = client.get("/api/cards").json()["cards"]

    assert cards[0]["ok"] is False
    assert any("角色名单" in message for message in cards[0]["errors"])


@pytest.mark.parametrize("name", ["index.html"])
def test_the_ui_file_is_packaged(name: str) -> None:
    """The page ships with the package so `serve` works after installation."""
    page = Path(__file__).resolve().parents[1] / "playtest" / "web" / name

    assert page.exists()
    assert "api/runs" in page.read_text(encoding="utf-8")

"""Pre-flight checks: server, DM credential and seat endpoints.

Every check runs against an injected probe, so these tests never open a socket.
"""

from __future__ import annotations

import json
from pathlib import Path
from typing import Any

import httpx
import pytest

from playtest.card import inspect_card
from playtest.cli import EXIT_INVALID, EXIT_OK, main
from playtest.config import PlaytestConfig
from playtest.doctor import (
    CheckResult,
    describe_error,
    model_names,
    run_checks,
    run_checks_sync,
    summarize,
)
from test_playtest_runner import round_card, write_card

MODELS_BODY = '{"data": [{"id": "deepseek-chat"}, {"id": "deepseek-reasoner"}]}'


def make_probe(responses: dict[str, tuple[int, str]]) -> Any:
    """Return a probe that answers from a URL prefix table."""
    calls: list[tuple[str, str | None]] = []

    async def probe(
        url: str, api_key: str | None, verify_tls: bool, use_proxy: bool
    ) -> tuple[int, str]:
        del verify_tls, use_proxy
        calls.append((url, api_key))
        for prefix, answer in responses.items():
            if url.startswith(prefix):
                return answer
        return 0, "无法连接：没有为这个 URL 配置应答"

    probe.calls = calls  # type: ignore[attr-defined]
    return probe


def build_config(tmp_path: Path, **overrides: Any) -> PlaytestConfig:
    """Return a configuration with two independently configured seats."""
    raw: dict[str, Any] = {
        "server": {"url": "https://127.0.0.1:4141"},
        "dm": {
            "mode": "own",
            "endpoint": "https://dm.example.com/v1",
            "api_key": "dm-key",
            "model_name": "deepseek-chat",
        },
        "seats": {
            "default": {
                "endpoint": "https://seat.example.com/v1",
                "api_key": "seat-key",
                "model_name": "deepseek-chat",
            }
        },
        "run": {"dry_run": False},
    }
    for key, value in overrides.items():
        if key == "seats" and isinstance(value, dict):
            seats = {name: dict(entry) for name, entry in raw["seats"].items()}
            for name, entry in value.items():
                seats[name] = {**seats.get(name, {}), **entry} if isinstance(entry, dict) else entry
            raw["seats"] = seats
        elif isinstance(value, dict) and isinstance(raw.get(key), dict):
            raw[key] = {**raw[key], **value}
        else:
            raw[key] = value
    return PlaytestConfig.model_validate(raw)


def load_card(tmp_path: Path, payload: Any) -> Any:
    """Write and inspect a card."""
    card = inspect_card(write_card(tmp_path, payload))
    assert card.ok, card.findings
    return card


def test_a_fully_reachable_setup_passes(tmp_path: Path) -> None:
    """Every check is green when the server, DM and seats all answer."""
    card = load_card(tmp_path, round_card())
    probe = make_probe(
        {
            "https://127.0.0.1:4141/": (200, "200"),
            "https://dm.example.com": (200, "200，模型 2 个：deepseek-chat、deepseek-reasoner"),
            "https://seat.example.com": (
                200,
                "200，模型 2 个：deepseek-chat、deepseek-reasoner",
            ),
        }
    )

    results = run_checks_sync(build_config(tmp_path), card, probe=probe)

    levels = {result.name: result.level for result in results}
    assert levels["剧情卡"] == "ok"
    assert levels["服务器连通"] == "ok"
    assert levels["DM 凭据"] == "ok"
    assert all(level == "ok" for name, level in levels.items() if name.startswith("座位"))
    assert summarize(results) == (4, 0, 0)
    # Seat checks are deduplicated by endpoint and model.
    assert len(probe.calls) == 3


def test_an_unreachable_server_fails_the_check(tmp_path: Path) -> None:
    """A dead server blocks the run before any token is spent."""
    card = load_card(tmp_path, round_card())
    probe = make_probe({"https://dm.example.com": (200, "200")})

    results = run_checks_sync(build_config(tmp_path), card, probe=probe)

    server = next(item for item in results if item.name == "服务器连通")
    assert server.level == "fail"
    assert server.failed is True
    assert summarize(results)[2] >= 1


def test_a_rejected_seat_key_fails(tmp_path: Path) -> None:
    """A 401 on one seat endpoint is reported as a failure."""
    card = load_card(tmp_path, round_card())
    probe = make_probe(
        {
            "https://127.0.0.1:4141/": (200, "200"),
            "https://dm.example.com": (200, "200"),
            "https://seat.example.com": (401, "HTTP 401"),
        }
    )

    results = run_checks_sync(build_config(tmp_path), card, probe=probe)

    seat = next(item for item in results if item.name.startswith("座位"))
    assert seat.level == "fail"
    assert "拒绝" in seat.detail


def test_a_missing_model_warns_but_does_not_fail(tmp_path: Path) -> None:
    """An endpoint that answers without the configured model is a warning."""
    card = load_card(tmp_path, round_card())
    probe = make_probe(
        {
            "https://127.0.0.1:4141/": (200, "200"),
            "https://dm.example.com": (200, "200，模型 1 个：other-model"),
            "https://seat.example.com": (200, "200，模型 1 个：other-model"),
        }
    )

    results = run_checks_sync(build_config(tmp_path), card, probe=probe)

    assert all(item.level == "warn" for item in results if item.name == "DM 凭据")
    assert any(item.level == "warn" and "模型列表" in item.detail for item in results)
    assert summarize(results)[2] == 0


def test_a_seat_without_a_key_fails_without_a_request(tmp_path: Path) -> None:
    """A missing key is caught before the request is made."""
    card = load_card(tmp_path, round_card())
    config = build_config(
        tmp_path,
        seats={"default": {"api_key": None, "api_key_env": "PLAYTEST_ABSENT_KEY"}},
    )
    probe = make_probe(
        {
            "https://127.0.0.1:4141/": (200, "200"),
            "https://dm.example.com": (200, "200"),
        }
    )

    results = run_checks_sync(config, card, probe=probe)

    seat = next(item for item in results if item.name.startswith("座位"))
    assert seat.level == "fail"
    assert "api_key_env" in seat.detail
    assert all("seat.example.com" not in url for url, _ in probe.calls)


def test_sample_mode_warns_and_skips_seat_requests(tmp_path: Path) -> None:
    """A dry run needs no seat endpoints at all."""
    card = load_card(tmp_path, round_card())
    probe = make_probe(
        {
            "https://127.0.0.1:4141/": (200, "200"),
            "https://dm.example.com": (200, "200"),
        }
    )

    results = run_checks_sync(build_config(tmp_path, run={"dry_run": True}), card, probe=probe)

    assert any("dry-run" in item.detail for item in results)
    assert all("seat.example.com" not in url for url, _ in probe.calls)


def test_borrow_mode_is_reported_as_unverifiable(tmp_path: Path) -> None:
    """Borrowing the server key cannot be checked offline."""
    card = load_card(tmp_path, round_card())
    probe = make_probe({"https://127.0.0.1:4141/": (200, "200")})
    config = build_config(tmp_path, dm={"mode": "borrow"})

    results = run_checks_sync(config, card, probe=probe)

    dm = next(item for item in results if item.name == "DM 凭据")
    assert dm.level == "warn"
    assert "borrow" in dm.detail


def test_an_invalid_card_stops_the_checks_early(tmp_path: Path) -> None:
    """A card that cannot be played is reported without touching the network."""
    card = inspect_card(write_card(tmp_path, {"prompt_blocks": []}))
    probe = make_probe({})

    results = asyncio_run(run_checks(build_config(tmp_path), card, probe=probe))

    assert len(results) == 1
    assert results[0].name == "剧情卡"
    assert results[0].level == "fail"
    assert probe.calls == []


def asyncio_run(coroutine: Any) -> Any:
    """Run one coroutine for the invalid-card check."""
    import asyncio

    return asyncio.run(coroutine)


def test_model_names_reads_the_openai_models_shape() -> None:
    """Model ids are extracted for a helpful message."""
    assert model_names(200, "200，模型 2 个：a、b") == ["a", "b"]
    assert model_names(200, "200") == []
    assert model_names(401, "200，模型 1 个：a") == []


def test_describe_error_never_returns_an_empty_string() -> None:
    """httpx connect errors stringify empty, so the type name is used."""
    empty = httpx.ConnectError("")
    assert describe_error(empty) == "ConnectError"
    wrapped = httpx.ConnectError("")
    wrapped.__cause__ = OSError("connection refused")
    assert "connection refused" in describe_error(wrapped)
    assert describe_error(ValueError("boom")) == "boom"


def test_check_results_are_json_serializable() -> None:
    """The check payload can be consumed by scripts."""
    payload = {
        "ok": True,
        "checks": [CheckResult("剧情卡", "ok", "《x》").as_dict()],
    }
    assert json.loads(json.dumps(payload, ensure_ascii=False))["checks"][0]["level"] == "ok"


def test_the_check_command_reports_and_exits(tmp_path: Path, capsys: Any) -> None:
    """The CLI wires the checks, prints them and exits non-zero on failure."""
    card = write_card(tmp_path, round_card())
    config = tmp_path / "playtest.yaml"
    config.write_text(
        "server:\n  url: 'https://127.0.0.1:1'\n" "run:\n  dry_run: true\n" f"card: '{card}'\n",
        encoding="utf-8",
    )

    code = main(["check", "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_INVALID
    assert payload["ok"] is False
    assert payload["summary"]["fail"] >= 1

    with pytest.raises(SystemExit):
        main(["check"])  # --config is required


def test_the_check_command_passes_for_a_reachable_server(
    tmp_path: Path, capsys: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """A reachable server with dry-run seats is enough to proceed."""
    card = write_card(tmp_path, round_card())
    config = tmp_path / "playtest.yaml"
    config.write_text(
        "server:\n  url: 'https://127.0.0.1:4141'\nrun:\n  dry_run: true\n" f"card: '{card}'\n",
        encoding="utf-8",
    )

    async def fake_probe(
        url: str, api_key: str | None, verify_tls: bool, use_proxy: bool
    ) -> tuple[int, str]:
        del api_key, verify_tls, use_proxy
        return (200, "200") if url.startswith("https://127.0.0.1:4141/") else (0, "no route")

    monkeypatch.setattr("playtest.doctor.http_probe", fake_probe)

    code = main(["check", "--config", str(config)])
    output = capsys.readouterr().out

    assert code == EXIT_OK
    assert "✓ 服务器连通" in output
    assert "可以开始真实跑测" in output

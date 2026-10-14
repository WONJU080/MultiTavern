"""The ``playtest validate`` CLI: exit codes, output and config cross-checks."""

import json
from pathlib import Path
from typing import Any

import pytest

from playtest.cli import EXIT_INVALID, EXIT_OK, EXIT_USAGE, EXIT_WARNINGS, main

REPO_ROOT = Path(__file__).resolve().parents[1]


def good_card(tmp_path: Path, name: str = "card.anyworld.json") -> Path:
    """Write a card that passes every check."""
    payload: dict[str, Any] = {
        "scenario": "【剧本：好卡】\n\n一间上锁的门。",
        "guidance": "真相：钥匙在花盆下面。",
        "characters": [
            {"name": "甲", "description": "谨慎的锁匠"},
            {"name": "乙", "description": "急躁的记者"},
        ],
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def warned_card(tmp_path: Path, name: str = "warned.anyworld.json") -> Path:
    """Write a card the server accepts but that has a design warning."""
    payload: dict[str, Any] = {
        "scenario": "【剧本：警告卡】\n\n一间上锁的门。",
        "guidance": "真相：钥匙在花盆下面。",
        "characters": [{"name": "甲", "description": "锁匠"}, {"name": "乙"}],
        "time_config": {"enabled": True, "start_minute": 600},
        "regions": [
            {"id": "hall", "name": "门厅", "items": [], "links": [{"to": "study"}]},
            {"id": "study", "name": "书房", "items": [], "links": []},
            {"id": "attic", "name": "阁楼", "items": [], "links": []},
        ],
        "world": {"start_region": "hall"},
    }
    path = tmp_path / name
    path.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return path


def write_config(tmp_path: Path, text: str) -> Path:
    """Write a playtest YAML config into the test's temporary directory."""
    path = tmp_path / "playtest.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_a_valid_card_exits_zero(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A clean card prints its summary and exits 0."""
    code = main(["validate", "--card", str(good_card(tmp_path))])
    output = capsys.readouterr().out

    assert code == EXIT_OK
    assert "通过服务器校验" in output
    assert "调度：round（轮流交卷）" in output
    assert "汇总：1 张卡，通过 1，失败 0" in output


def test_an_invalid_card_exits_one(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """A card the server would reject exits 1."""
    path = tmp_path / "preset.anyworld.json"
    path.write_text(json.dumps({"prompt_blocks": []}), encoding="utf-8")

    code = main(["validate", "--card", str(path)])
    output = capsys.readouterr().out

    assert code == EXIT_INVALID
    assert "not_a_card" in output
    assert "失败 1" in output


def test_strict_mode_turns_warnings_into_a_failure(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--strict makes card hygiene enforceable in scripts."""
    card = str(warned_card(tmp_path))

    assert main(["validate", "--card", card]) == EXIT_OK
    capsys.readouterr()
    assert main(["validate", "--card", card, "--strict"]) == EXIT_WARNINGS


def test_json_output_is_machine_readable(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """--json prints one JSON document and nothing else."""
    card = str(warned_card(tmp_path))
    code = main(["validate", "--card", card, "--json"])
    payload = json.loads(capsys.readouterr().out)

    assert code == EXIT_OK
    assert payload["ok"] is True
    assert payload["summary"] == {
        "total": 1,
        "passed": 1,
        "failed": 0,
        "warnings": 1,
        "infos": 2,
    }
    card_payload = payload["cards"][0]
    assert card_payload["schedule"] == "strict"
    levels = {finding["code"]: finding["level"] for finding in card_payload["findings"]}
    assert levels["unreachable_region"] == "warning"
    assert levels["thin_character"] == "info"


def test_quiet_hides_non_error_findings(tmp_path: Path, capsys: pytest.CaptureFixture[str]) -> None:
    """--quiet keeps the summary but drops warnings and notes."""
    card = str(warned_card(tmp_path))
    main(["validate", "--card", card, "--quiet"])
    output = capsys.readouterr().out

    assert "unreachable_region" not in output
    assert "汇总：" in output


def test_usage_errors_exit_two(capsys: pytest.CaptureFixture[str], tmp_path: Path) -> None:
    """Missing inputs and a bad directory are usage errors, not card failures."""
    assert main(["validate"]) == EXIT_USAGE
    assert "至少需要 --card" in capsys.readouterr().err

    assert main(["validate", "--cards", str(tmp_path / "absent")]) == EXIT_USAGE
    assert "不是目录" in capsys.readouterr().err


def test_a_broken_config_is_a_usage_error(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A config typo fails before any card is inspected."""
    config = write_config(tmp_path, "seats:\n  defualt:\n    model_name: m\n")

    code = main(["validate", "--card", str(good_card(tmp_path)), "--config", str(config)])

    assert code == EXIT_USAGE
    assert "seats.defualt" in capsys.readouterr().err


def test_config_cross_check_reports_seat_problems(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """Seat gaps and stale overrides are caught without running anything."""
    card = good_card(tmp_path)
    config = write_config(
        tmp_path,
        'card: "card.anyworld.json"\n'
        "seats:\n"
        "  default:\n"
        "    style: rotate\n"
        "  overrides:\n"
        "    Ghost:\n"
        "      model_name: m\n",
    )

    code = main(["validate", "--card", str(card), "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)
    found = {finding["code"] for finding in payload["cards"][0]["findings"]}

    assert code == EXIT_OK
    assert {"unknown_seat_override", "seat_incomplete", "dm_borrow"} <= found


def test_config_cross_check_reports_a_missing_dm_key(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A host-supplied DM credential must be complete before a run starts."""
    card = good_card(tmp_path)
    config = write_config(
        tmp_path,
        "dm:\n  mode: own\nseats:\n  default:\n    endpoint: https://x/v1\n    model_name: m\n",
    )

    main(["validate", "--card", str(card), "--config", str(config), "--json", "--quiet"])
    payload = json.loads(capsys.readouterr().out)
    found = {finding["code"] for finding in payload["cards"][0]["findings"]}

    assert {"dm_endpoint_missing", "dm_key_missing"} <= found


def test_dry_run_skips_seat_completeness(
    tmp_path: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A dry run drives scripted seats, so seat LLM settings are optional."""
    card = good_card(tmp_path)
    config = write_config(tmp_path, "run:\n  dry_run: true\n")

    main(["validate", "--card", str(card), "--config", str(config), "--json"])
    payload = json.loads(capsys.readouterr().out)
    found = {finding["code"] for finding in payload["cards"][0]["findings"]}

    assert "seat_incomplete" not in found


def test_the_console_script_targets_resolve() -> None:
    """pyproject ships `anyworld-playtest`; the documented targets must exist."""
    import importlib
    import tomllib

    project = tomllib.loads((REPO_ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    scripts = project["project"]["scripts"]
    assert scripts["anyworld-playtest"] == "playtest.cli:main"
    assert "playtest" in project["tool"]["hatch"]["build"]["targets"]["wheel"]["packages"]

    module_name, _, attribute = scripts["anyworld-playtest"].partition(":")
    target = getattr(importlib.import_module(module_name), attribute)
    assert callable(target)


def test_a_subcommand_is_required() -> None:
    """Calling the tool without a sub-command is an argparse usage error."""
    with pytest.raises(SystemExit):
        main([])

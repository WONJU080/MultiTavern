"""Run-configuration parsing for the playtest driver."""

from pathlib import Path

import pytest

from playtest.config import (
    Credential,
    PlaytestConfig,
    SeatLLM,
    SeatSettings,
    load_config,
    plan_seats,
    resolve_api_key,
)

REPO_ROOT = Path(__file__).resolve().parents[1]
EXAMPLE_CONFIG = REPO_ROOT / "playtest" / "configs" / "example.yaml"


def write_config(tmp_path: Path, text: str) -> Path:
    """Write a YAML config file into the test's temporary directory."""
    path = tmp_path / "playtest.yaml"
    path.write_text(text, encoding="utf-8")
    return path


def test_load_shipped_example_config() -> None:
    """The documented example config parses as-is."""
    config = load_config(EXAMPLE_CONFIG)

    assert isinstance(config, PlaytestConfig)
    assert config.server.url == "https://127.0.0.1:4141"
    assert config.server.verify_tls is False
    assert config.dm.mode == "borrow"
    assert config.seats.default.endpoint == "https://api.deepseek.com/v1"
    assert config.seats.default.api_key_env == "DEEPSEEK_API_KEY"
    assert config.seats.default.style == "rotate"
    assert config.run.max_rounds == 6
    assert config.report.formats == ["json", "markdown"]


def test_unknown_keys_are_rejected_before_a_run(tmp_path: Path) -> None:
    """A typo in the config must fail fast instead of being silently ignored."""
    path = write_config(tmp_path, "seats:\n  defualt:\n    model_name: x\n")

    with pytest.raises(ValueError) as excinfo:
        load_config(path)

    assert "seats.defualt" in str(excinfo.value)


def test_out_of_range_values_are_rejected(tmp_path: Path) -> None:
    """Seat sampling and run budgets are bounded."""
    path = write_config(tmp_path, "seats:\n  default:\n    temperature: 3\n")

    with pytest.raises(ValueError) as excinfo:
        load_config(path)

    assert "seats.default.temperature" in str(excinfo.value)


def test_url_must_be_http_or_https(tmp_path: Path) -> None:
    """A wrong scheme fails at load time, and a trailing slash is normalized."""
    bad = write_config(tmp_path, "server:\n  url: 'ws://127.0.0.1:4141'\n")
    with pytest.raises(ValueError) as excinfo:
        load_config(bad)
    assert "server.url" in str(excinfo.value)

    good = write_config(tmp_path, "server:\n  url: 'https://127.0.0.1:4141/'\n")
    assert load_config(good).server.url == "https://127.0.0.1:4141"


def test_missing_and_non_mapping_configs_are_rejected(tmp_path: Path) -> None:
    """An absent file and a YAML list both fail with a clear message."""
    with pytest.raises(ValueError) as excinfo:
        load_config(tmp_path / "absent.yaml")
    assert "无法读取配置文件" in str(excinfo.value)

    path = write_config(tmp_path, "- a\n- b\n")
    with pytest.raises(ValueError) as excinfo:
        load_config(path)
    assert "必须是 YAML 对象" in str(excinfo.value)


def test_overrides_merge_field_by_field_into_the_default() -> None:
    """An override may change one field and keep the rest of the default seat."""
    settings = SeatSettings.model_validate(
        {
            "default": {
                "endpoint": "https://api.example.com/v1",
                "api_key_env": "PLAYTEST_KEY",
                "model_name": "base-model",
                "temperature": 0.7,
                "style": "rotate",
            },
            "overrides": {"甲": {"model_name": "reasoner", "style": "bold"}},
        }
    )

    seat = settings.seat_for("甲")

    assert seat.model_name == "reasoner"
    assert seat.style == "bold"
    assert seat.endpoint == "https://api.example.com/v1"
    assert seat.temperature == 0.7
    assert seat.api_key_env == "PLAYTEST_KEY"
    assert seat.max_output_tokens == 800  # the default cap
    assert seat.is_complete() is True
    # The default seat itself is untouched by the merge.
    assert settings.default.model_name == "base-model"
    assert settings.default.style == "rotate"


def test_plan_seats_keeps_cast_order_and_rotates_styles() -> None:
    """Unset styles rotate so several seats do not behave identically."""
    settings = SeatSettings.model_validate(
        {
            "default": {"style": "rotate"},
            "overrides": {"丙": {"style": "cautious"}},
        }
    )

    plans = plan_seats(settings, ["甲", "乙", "丙", "丁"])

    assert [plan.character for plan in plans] == ["甲", "乙", "丙", "丁"]
    assert [plan.style for plan in plans] == ["explore", "social", "cautious", "bold"]


def test_unknown_overrides_are_reported() -> None:
    """Overrides that name no cast member are surfaced, not silently dropped."""
    settings = SeatSettings.model_validate({"overrides": {"甲": {}, "Ghost": {}}})

    assert settings.unknown_overrides(["甲", "乙"]) == ["Ghost"]


def test_resolve_api_key_prefers_the_environment(monkeypatch: pytest.MonkeyPatch) -> None:
    """Environment variables win, and an unset variable falls back to the literal."""
    monkeypatch.setenv("PLAYTEST_KEY", "from-env")
    assert resolve_api_key("literal", "PLAYTEST_KEY") == "from-env"
    assert resolve_api_key(None, "PLAYTEST_MISSING_KEY") is None
    assert resolve_api_key("literal", "PLAYTEST_MISSING_KEY") == "literal"


def test_proxying_is_opt_in() -> None:
    """A self-hosted server is contacted directly unless the config says otherwise."""
    assert PlaytestConfig().server.proxy is False
    assert (
        PlaytestConfig.model_validate(
            {"server": {"url": "https://127.0.0.1:4141", "proxy": True}}
        ).server.proxy
        is True
    )


def test_the_transport_factory_carries_the_proxy_setting() -> None:
    """The runner hands the configured proxy policy to every connection."""
    from playtest.runner import websocket_transport_factory

    factory = websocket_transport_factory(
        PlaytestConfig.model_validate({"server": {"url": "https://127.0.0.1:4141"}})
    )
    transport = factory("2b0b8b1e-0000-4000-8000-000000000000")
    assert transport.use_proxy is False

    proxied = websocket_transport_factory(
        PlaytestConfig.model_validate({"server": {"url": "https://example.com", "proxy": True}})
    )("2b0b8b1e-0000-4000-8000-000000000001")
    assert proxied.use_proxy is True


def test_credential_and_seat_completeness() -> None:
    """Completeness checks never require the key to be printed."""
    assert Credential(mode="borrow").is_complete() is False
    assert Credential(mode="own", endpoint="https://x/v1").is_complete() is False
    assert Credential(mode="own", endpoint="https://x/v1", api_key="k").is_complete() is True
    assert SeatLLM(endpoint="https://x/v1", model_name="m").is_complete() is True
    assert SeatLLM(endpoint="https://x/v1").is_complete() is False

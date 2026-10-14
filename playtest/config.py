"""Run configuration for the offline playtest driver.

The models below describe one playtest run: which server to talk to, which
credential drives the room's DM, one LLM seat per cast member, the run budgets
and the report output. Unknown keys are rejected so a typo fails before any
tokens are spent.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Literal, Sequence

import yaml
from pydantic import BaseModel, ConfigDict, Field, ValidationError, field_validator

# Player styles keep several seats from behaving like one model with one prompt.
STYLE_ROTATION: tuple[str, ...] = ("explore", "social", "bold", "cautious", "orderly")
SeatStyle = Literal["explore", "social", "bold", "cautious", "orderly", "rotate"]


class ConfigModel(BaseModel):
    """Base config model that rejects unknown keys."""

    model_config = ConfigDict(extra="forbid")


def resolve_api_key(api_key: str | None, api_key_env: str | None) -> str | None:
    """Return the environment key when present, else the literal key."""
    if api_key_env:
        value = os.environ.get(api_key_env)
        if value:
            return value
    return api_key or None


def format_validation_error(error: ValidationError) -> str:
    """Render a pydantic error as one bounded, location-first line."""
    lines = []
    for item in error.errors():
        location = ".".join(str(part) for part in item.get("loc", ())) or "<root>"
        lines.append(f"{location}: {item.get('msg', 'invalid value')}")
    return "; ".join(lines)


class ServerTarget(ConfigModel):
    """The running Anyworld server the seats connect to."""

    url: str = "https://127.0.0.1:4141"
    verify_tls: bool = False
    # A self-hosted game server is reached directly. Following the system proxy
    # would route loopback traffic through it (and would demand python-socks for
    # a SOCKS proxy), so proxying is opt-in.
    proxy: bool = False

    @field_validator("url")
    @classmethod
    def _validate_url(cls, value: str) -> str:
        if not value.startswith(("http://", "https://")):
            raise ValueError("必须以 http:// 或 https:// 开头")
        return value.rstrip("/")


class Credential(ConfigModel):
    """The room DM credential: borrow the server's key or supply one."""

    mode: Literal["borrow", "own"] = "borrow"
    endpoint: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    provider: Literal["compatible", "openai"] | None = None
    model_name: str | None = None

    def resolved_api_key(self) -> str | None:
        """Return the effective API key without printing it."""
        return resolve_api_key(self.api_key, self.api_key_env)

    def is_complete(self) -> bool:
        """Return whether this credential can create a room on its own."""
        return bool(self.endpoint) and bool(self.resolved_api_key())


class SeatLLM(ConfigModel):
    """One player seat's LLM settings."""

    endpoint: str | None = None
    api_key: str | None = None
    api_key_env: str | None = None
    provider: Literal["compatible", "openai"] | None = None
    model_name: str | None = None
    temperature: float | None = Field(default=None, ge=0, le=2)
    max_output_tokens: int = Field(default=800, ge=16, le=32_000)
    # A player types one short action, not a paragraph.
    action_limit: int = Field(default=80, ge=20, le=2_000)
    style: SeatStyle | None = None
    # None = follow the system proxy unless the endpoint is local (default);
    # True = always proxy; False = always direct.
    proxy: bool | None = None

    def resolved_api_key(self) -> str | None:
        """Return the effective API key without printing it."""
        return resolve_api_key(self.api_key, self.api_key_env)

    def is_complete(self) -> bool:
        """Return whether the seat names a backend to call."""
        return bool(self.endpoint) and bool(self.model_name)


class SeatSettings(ConfigModel):
    """One default seat plus per-character overrides."""

    default: SeatLLM = Field(default_factory=SeatLLM)
    overrides: dict[str, SeatLLM] = Field(default_factory=dict)

    def seat_for(self, character: str) -> SeatLLM:
        """Return the effective settings for one character.

        Overrides merge field by field into ``default``: only fields the
        override actually sets replace the default, so a seat may change just
        its model, temperature or style.
        """
        override = self.overrides.get(character)
        if override is None:
            return self.default
        explicit = override.model_dump(exclude_unset=True, exclude_none=True)
        if not explicit:
            return self.default
        return self.default.model_copy(update=explicit)

    def unknown_overrides(self, characters: Sequence[str]) -> list[str]:
        """Return override keys that do not name a cast member."""
        known = {name.casefold() for name in characters}
        return [name for name in self.overrides if name.casefold() not in known]


@dataclass(frozen=True, slots=True)
class SeatPlan:
    """The resolved seat for one character: settings plus a player style."""

    character: str
    style: str
    llm: SeatLLM
    host_seat: bool = False


def plan_seats(
    settings: SeatSettings,
    characters: Sequence[str],
    *,
    host_character: str | None = None,
) -> list[SeatPlan]:
    """Assign every character a seat, rotating styles for unset ones.

    Distinct styles matter more than distinct models: several seats sharing one
    prompt and one model tend to submit the same action, which tests nothing.
    ``host_character`` is driven by the host connection instead of its own.
    """
    plans: list[SeatPlan] = []
    rotation = 0
    for character in characters:
        llm = settings.seat_for(character)
        style = llm.style or ""
        if not style or style == "rotate":
            style = STYLE_ROTATION[rotation % len(STYLE_ROTATION)]
            rotation += 1
        plans.append(
            SeatPlan(
                character=character,
                style=style,
                llm=llm,
                host_seat=bool(host_character) and character == host_character,
            )
        )
    return plans


class RunLimits(ConfigModel):
    """Hard budgets that stop a run before it burns unbounded tokens or time."""

    max_rounds: int = Field(default=6, ge=1, le=500)
    max_minutes: float = Field(default=30.0, gt=0, le=1440)
    max_total_tokens: int | None = Field(default=None, gt=0)
    seat_timeout_seconds: float = Field(default=60.0, gt=0, le=600)
    stall_seconds: float = Field(default=180.0, gt=0, le=3600)
    # How often the host may ask the DM to redo a failed round resolution.
    max_round_retries: int = Field(default=2, ge=0, le=10)
    dry_run: bool = False
    # Write every seat prompt to the report directory for inspection.
    debug_prompts: bool = False


class ReportOptions(ConfigModel):
    """Where and how the run report is written."""

    out_dir: str = "playtest-runs"
    formats: list[Literal["json", "markdown"]] = Field(default_factory=lambda: ["json", "markdown"])


class PlaytestConfig(ConfigModel):
    """The complete configuration of one playtest run."""

    server: ServerTarget = Field(default_factory=ServerTarget)
    card: str | None = None
    dm: Credential = Field(default_factory=Credential)
    seats: SeatSettings = Field(default_factory=SeatSettings)
    run: RunLimits = Field(default_factory=RunLimits)
    report: ReportOptions = Field(default_factory=ReportOptions)


def load_config(path: str | Path) -> PlaytestConfig:
    """Read a YAML run configuration, rejecting unknown keys and bad types."""
    config_path = Path(path)
    try:
        text = config_path.read_text(encoding="utf-8")
    except OSError as exc:
        raise ValueError(f"无法读取配置文件 {config_path}: {exc}") from exc
    try:
        raw: Any = yaml.safe_load(text)
    except yaml.YAMLError as exc:
        raise ValueError(f"配置文件不是合法 YAML: {exc}") from exc
    if raw is None:
        raw = {}
    if not isinstance(raw, dict):
        raise ValueError("配置文件必须是 YAML 对象。")
    try:
        return PlaytestConfig.model_validate(raw)
    except ValidationError as exc:
        raise ValueError(f"配置校验失败 -> {format_validation_error(exc)}") from exc

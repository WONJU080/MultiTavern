"""Seat brains: how one AI player decides what its character does.

A brain is deliberately narrow. It receives one bounded, player-visible
briefing and returns one in-world action; it never sees DM guidance, other
seats' reasoning, or party chat. That keeps every seat comparable to a human
sitting at the same table, and keeps a five-seat card test affordable.
"""

from __future__ import annotations

import asyncio
import logging
import re
import zlib
from dataclasses import dataclass
from functools import lru_cache
from pathlib import Path
from string import Template
from typing import Any, Callable, Protocol, Sequence

from urllib.parse import urlsplit

from core.schemas import Character
from playtest.config import SeatLLM
from playtest.transport import is_local_address

LOGGER = logging.getLogger(__name__)

PROMPT_DIR = Path(__file__).resolve().parent / "prompts" / "zh"
DEFAULT_ACTION_LIMIT = 80
MAX_OUTPUT_CEILING = 4_000

STYLE_HINTS: dict[str, str] = {
    "explore": "优先探索与调查：主动察看陌生地点、翻找可疑物品，把未知变成已知。",
    "social": "优先与人打交道：搭话、试探、交换情报、结盟或施压，从别人嘴里拿信息。",
    "bold": "果断直接：倾向立刻行动、冒险与正面冲突，愿意为推进剧情承担风险。",
    "cautious": "谨慎稳妥：先观察与确认再动手，避免无准备的冲突，始终保留退路。",
    "orderly": "讲规则与秩序：组织分工、制定计划、守约与照看同伴，反对各自为政。",
}
DEFAULT_STYLE_HINT = "按角色卡的性格行事，主动推进剧情，不做被动的旁观者。"

# Offline fallback actions: deterministic, varied per round, never meta.
SCRIPTED_ACTIONS: tuple[str, ...] = (
    "我先看看四周有什么。",
    "我检查最近的那件东西。",
    "我问同伴刚才看到了什么。",
    "我走到门口确认出路。",
)

_OOC_PATTERNS = (
    re.compile(r"^\s*[（(]\s*(?:ooc|OOC|系统|旁白)\s*[:：][^）)]*[）)]\s*", re.IGNORECASE),
    re.compile(r"^\s*(?:ooc|OOC)\s*[:：].*$", re.MULTILINE),
)
_LABEL_PATTERN = re.compile(
    r"^\s*(?:我的)?(?:行动|动作|提交|行动内容|action)\s*[:：]\s*", re.IGNORECASE
)
_FENCE_PATTERN = re.compile(r"^\s*```[a-zA-Z]*\s*|\s*```\s*$")


def use_system_proxy(seat: SeatLLM) -> bool:
    """Return whether this seat's HTTP calls should use the system proxy.

    ``proxy`` wins when set; otherwise a local endpoint is contacted directly
    and anything else follows the environment.
    """
    if seat.proxy is not None:
        return bool(seat.proxy)
    host = urlsplit(seat.endpoint or "").hostname or ""
    return not is_local_address(host)


class SeatError(RuntimeError):
    """Raised when a seat cannot produce an action."""


class SeatAuthError(SeatError):
    """Raised when the provider rejects this seat's API key.

    A bad key is not transient: retrying it only spends time and quota, so the
    runner stops the whole run instead of falling back again and again.
    """


@dataclass(frozen=True, slots=True)
class SeatTurn:
    """One request for an action, built only from player-visible state."""

    character: str
    style: str
    digest: str
    round_number: int
    action_limit: int = DEFAULT_ACTION_LIMIT
    rejection: str = ""


class SeatBrain(Protocol):
    """Decides one character's action for one turn."""

    async def act(self, turn: SeatTurn) -> str:
        """Return one non-empty in-world action."""
        ...


@dataclass(frozen=True, slots=True)
class TruthRequest:
    """One revealed possible-truth question this seat may answer."""

    character: str
    style: str
    question_id: str
    question_text: str
    digest: str
    round_number: int = 0
    max_chars: int = 500


class TruthBrain(Protocol):
    """A seat that can also answer a revealed possible-truth question."""

    async def answer_truth(self, request: TruthRequest) -> str:
        """Return one declarative in-world claim, or an empty string to abstain."""
        ...


@lru_cache(maxsize=8)
def _load_template(name: str) -> Template:
    """Load one prompt template from disk."""
    return Template((PROMPT_DIR / f"{name}.md").read_text(encoding="utf-8"))


def render_prompt(name: str, **values: Any) -> str:
    """Render a prompt template with safe substitution."""
    return _load_template(name).safe_substitute(**values)


def character_card(character: Character) -> str:
    """Render the public part of a cast member for a seat prompt."""
    parts = [f"姓名：{character.name}"]
    if character.description.strip():
        parts.append(f"背景：{character.description.strip()}")
    if character.personality.strip():
        parts.append(f"性格：{character.personality.strip()}")
    if character.style.strip():
        parts.append(f"说话风格：{character.style.strip()}")
    if character.example_dialogue.strip():
        parts.append(f"示例台词：{character.example_dialogue.strip()}")
    return "\n".join(parts)


def style_hint(style: str) -> str:
    """Return the behaviour hint for one player style."""
    return STYLE_HINTS.get(style, DEFAULT_STYLE_HINT)


def build_system_prompt(character: Character, style: str, action_limit: int) -> str:
    """Build one seat's stable system prompt."""
    return render_prompt(
        "seat_system",
        character=character.name,
        character_card=character_card(character),
        style_hint=style_hint(style),
        action_limit=action_limit,
        digest="（见下一条消息）",
    )


def build_turn_prompt(turn: SeatTurn) -> str:
    """Build the user message for one turn, including the retry hint."""
    if turn.rejection:
        return render_prompt(
            "action_retry",
            context=f"第{turn.round_number}回合",
            digest=turn.digest,
            reason=turn.rejection,
        )
    return (
        f"{turn.digest}\n\n"
        "——\n"
        f"现在轮到你（{turn.character}）行动（第 {turn.round_number} 回合）。\n"
        "只写你这一轮的行动：第一人称「我」，通常 1 句、最多 2 句，写清你对谁/对什么做什么。\n"
        "只写你的尝试——不要写结果、不要写你发现了什么、不要编造 DM 没写过的设定。\n"
        "不要写第三人称旁白，不要复述上面的叙述，不要替其他角色行动或说话。"
    )


def _truncate_question(text: str, limit: int = 60) -> str:
    """Collapse a possible-truth question for use inside a claim."""
    collapsed = " ".join(str(text).split())
    return collapsed if len(collapsed) <= limit else collapsed[: limit - 1] + "…"


def build_truth_prompt(request: TruthRequest) -> str:
    """Build the user message asking one seat to answer a truth question."""
    return render_prompt(
        "truth_answer",
        character=request.character,
        question=request.question_text,
        max_chars=request.max_chars,
        digest=request.digest,
    )


def clean_action(text: str, *, limit: int = DEFAULT_ACTION_LIMIT) -> str:
    """Normalize a model reply into one submit-ready action string.

    Returns an empty string when nothing usable is left, which the caller
    treats as a failed seat call rather than sending junk to the server.
    """
    if not isinstance(text, str):
        return ""
    cleaned = _FENCE_PATTERN.sub("", text.strip())
    for pattern in _OOC_PATTERNS:
        cleaned = pattern.sub("", cleaned)
    cleaned = _LABEL_PATTERN.sub("", cleaned.strip())
    cleaned = _strip_wrapping(cleaned.strip())
    cleaned = " ".join(cleaned.split())
    if not cleaned:
        return ""
    if len(cleaned) > limit:
        # Keep whole sentences when they fit, so a trimmed action still reads like
        # something a player typed instead of a sentence cut in half.
        shortened = _whole_sentences(cleaned, limit)
        tidied = _tidy_tail(shortened) if shortened else ""
        if tidied:
            return tidied
        # The ellipsis counts toward the limit so a seat never exceeds its budget.
        cut = cleaned[: max(1, limit - 1)]
        return (_balance_quotes(cut) or cut).rstrip() + "…"
    return cleaned


_QUOTE_PAIRS = {"「": "」", "『": "』", "“": "”", "（": "）", "(": ")", "【": "】"}
_CLOSERS = set(_QUOTE_PAIRS.values()) | {"”", "）", ")", "】"}


def _strip_wrapping(text: str) -> str:
    """Strip one *matched* pair of surrounding quotes, never a lone closer.

    A player action often ends with a spoken line: ``我问：「你几点到的？」``.
    Stripping both ends blindly would eat the closing bracket of the dialogue.
    """
    cleaned = text
    while len(cleaned) >= 2 and _QUOTE_PAIRS.get(cleaned[0]) == cleaned[-1]:
        cleaned = cleaned[1:-1].strip()
    return cleaned


def _balance_quotes(text: str) -> str:
    """Drop a trailing unfinished quote segment."""
    cleaned = text.rstrip("，,、；;：: ")
    for opener, closer in _QUOTE_PAIRS.items():
        while cleaned.count(opener) > cleaned.count(closer):
            index = cleaned.rfind(opener)
            if index <= 0:
                return ""
            cleaned = cleaned[:index].rstrip("，,、；;：: ")
    return cleaned


def _tidy_tail(text: str) -> str:
    """Close an unfinished quote and end the action with a full stop."""
    cleaned = _balance_quotes(text)
    if cleaned and cleaned[-1] not in "。！？!?…" and cleaned[-1] not in _CLOSERS:
        cleaned += "。"
    return cleaned


def _whole_sentences(text: str, limit: int) -> str:
    """Return as many leading whole sentences (or clauses) as fit the limit.

    Sentence boundaries are preferred; a run-on reply falls back to clause
    boundaries so the trimmed action still ends somewhere a reader would pause.
    """
    for pattern in (r"(?<=[。！？!?])", r"(?<=[，,、；;：:])"):
        kept = ""
        for part in re.split(pattern, text):
            if not part:
                continue
            if len(kept) + len(part) > limit:
                break
            kept += part
        if kept.strip():
            return kept.strip()
    return ""


def stable_offset(label: str, modulo: int) -> int:
    """Return a deterministic offset, stable across processes and machines."""
    if modulo <= 1:
        return 0
    return zlib.crc32(label.encode("utf-8")) % modulo


class ScriptedBrain:
    """A deterministic seat used by ``--dry-run`` and offline tests."""

    def __init__(
        self,
        actions: Sequence[str] | None = None,
        *,
        start_index: int = 0,
        action_limit: int = DEFAULT_ACTION_LIMIT,
    ) -> None:
        """Store the action rotation and this seat's phase into it."""
        self.actions = tuple(actions) if actions else SCRIPTED_ACTIONS
        self.start_index = start_index
        self.action_limit = action_limit
        self.calls = 0
        self.truth_calls = 0

    async def act(self, turn: SeatTurn) -> str:
        """Return the next scripted action, varied by round and seat."""
        index = (max(1, turn.round_number) - 1 + self.start_index) % len(self.actions)
        self.calls += 1
        return clean_action(self.actions[index], limit=turn.action_limit)

    async def answer_truth(self, request: TruthRequest) -> str:
        """Return a fixed declarative claim so dry runs exercise the plugin."""
        self.truth_calls += 1
        claim = f"关于「{_truncate_question(request.question_text)}」，我知道的都说了，我没有对任何人动手。"
        return clean_action(f"{request.character}说：{claim}", limit=request.max_chars)


class OpenAICompatBrain:
    """A seat backed by an OpenAI-compatible chat completions endpoint."""

    def __init__(
        self,
        seat: SeatLLM,
        character: Character,
        style: str,
        *,
        action_limit: int = DEFAULT_ACTION_LIMIT,
        timeout: float = 60.0,
        client: Any = None,
        response_sink: Callable[[str], None] | None = None,
    ) -> None:
        """Build the seat's client options; the API call happens in ``act``."""
        self.seat = seat
        self.character = character
        self.style = style
        self.action_limit = action_limit
        self.timeout = timeout
        self.response_sink = response_sink
        self._client = client
        self.system_prompt = build_system_prompt(character, style, action_limit)

    def _get_client(self) -> Any:
        """Create the OpenAI client lazily, mirroring the room's own client."""
        if self._client is None:
            import httpx
            from openai import AsyncOpenAI

            options: dict[str, Any] = {
                "api_key": self.seat.resolved_api_key(),
                "timeout": self.timeout,
                "max_retries": 0,
            }
            if not use_system_proxy(self.seat):
                # httpx follows the system proxy by default, which cannot reach a
                # loopback or LAN endpoint; keep those connections direct.
                options["http_client"] = httpx.AsyncClient(timeout=self.timeout, trust_env=False)
            if self.seat.endpoint:
                options["base_url"] = self.seat.endpoint
            elif self.seat.provider != "openai":
                raise SeatError(
                    f"座位 {self.character.name} 没有配置 endpoint。"
                    "留空会打到 OpenAI 官方接口；请填上你实际使用的服务地址"
                    "（DeepSeek / llama.cpp / Ollama 等的 /v1 地址）。"
                )
            self._client = AsyncOpenAI(**options)
        return self._client

    async def act(self, turn: SeatTurn) -> str:
        """Ask this seat's model for one action and clean the reply."""
        if not self.seat.model_name:
            raise SeatError(f"座位 {turn.character} 没有配置 model_name。")
        reply = await self._complete(
            user=build_turn_prompt(turn),
            limit=turn.action_limit,
            what="行动",
            character=turn.character,
        )
        return reply

    async def answer_truth(self, request: TruthRequest) -> str:
        """Ask this seat's model for one declarative claim about a question."""
        if not self.seat.model_name:
            raise SeatError(f"座位 {request.character} 没有配置 model_name。")
        return await self._complete(
            user=build_truth_prompt(request),
            limit=max(1, request.max_chars),
            what="真相陈述",
            character=request.character,
        )

    async def _send(self, options: dict[str, Any], character: str) -> Any:
        """Send one request, converting provider failures into seat errors."""
        try:
            async with asyncio.timeout(self.timeout):
                return await self._get_client().chat.completions.create(**options)
        except SeatError:
            raise
        except TimeoutError as exc:
            raise SeatError(f"座位 {character} 调用超时（{self.timeout:.0f}s）。") from exc
        except Exception as exc:  # noqa: BLE001 - provider errors become seat failures
            status = getattr(exc, "status_code", None)
            if status in (401, 403):
                raise SeatAuthError(
                    f"座位 {character} 的 API Key 被拒绝（HTTP {status}）：{_shorten_error(exc)}"
                ) from exc
            raise SeatError(f"座位 {character} 调用失败：{_shorten_error(exc)}") from exc

    def _record_response(
        self, character: str, what: str, cleaned: str, diagnostics: dict[str, Any]
    ) -> None:
        """Keep the raw reply when the operator asked for debugging."""
        if self.response_sink is None:
            return
        try:
            self.response_sink(
                f"\n### {character} · {what}\n\n"
                f"- 诊断：{_describe_reply(diagnostics)}\n"
                f"- 清洗后：{cleaned or '（空）'}\n"
                f"- 原始内容：\n\n```\n{diagnostics.get('raw') or '（空）'}\n```\n"
            )
        except Exception:  # noqa: BLE001 - debugging must not break a run
            pass

    async def aclose(self) -> None:
        """Close an injected HTTP client, if this seat owns one."""
        client = self._client
        self._client = None
        closer = getattr(client, "close", None)
        if client is None or closer is None:
            return
        try:
            result = closer()
            if asyncio.iscoroutine(result):
                await result
        except Exception:  # noqa: BLE001 - closing must not fail a run
            pass

    async def _complete(self, *, user: str, limit: int, what: str, character: str) -> str:
        """Send one seat request and clean the reply into a submission."""
        cap_key = "max_tokens" if self.seat.provider != "openai" else "max_completion_tokens"
        options: dict[str, Any] = {
            "model": self.seat.model_name,
            "messages": [
                {"role": "system", "content": self.system_prompt},
                {"role": "user", "content": user},
            ],
            cap_key: self.seat.max_output_tokens,
        }
        if self.seat.temperature is not None:
            options["temperature"] = self.seat.temperature
        attempted_larger = False
        response = await self._send(options, character)
        text, diagnostics = _read_reply(response)
        cleaned = clean_action(text, limit=limit)
        self._record_response(character, what, cleaned, diagnostics)
        if cleaned:
            return cleaned
        if not attempted_larger and diagnostics["truncated"]:
            # A reasoning-heavy model can spend the whole cap before writing the
            # action; one retry with more room is cheaper than a fallback action.
            attempted_larger = True
            options[cap_key] = min(MAX_OUTPUT_CEILING, int(options[cap_key]) * 2)
            response = await self._send(options, character)
            text, diagnostics = _read_reply(response)
            cleaned = clean_action(text, limit=limit)
            self._record_response(character, what, cleaned, diagnostics)
            if cleaned:
                return cleaned
        raise SeatError(f"座位 {character} 返回了空{what}（{_describe_reply(diagnostics)}）。")


def _shorten_error(exc: BaseException, limit: int = 300) -> str:
    """Keep one provider error readable in a progress line."""
    message = " ".join(str(exc).split())
    if len(message) <= limit:
        return message
    return message[: limit - 1] + "…"


def _read_reply(response: Any) -> tuple[str, dict[str, Any]]:
    """Return the assistant text plus diagnostics for an unusable reply."""
    choices = getattr(response, "choices", None) or []
    choice = choices[0] if choices else None
    message = getattr(choice, "message", None)
    finish_reason = str(getattr(choice, "finish_reason", "") or "")
    reasoning = getattr(message, "reasoning_content", None) if message is not None else None
    usage = getattr(response, "usage", None)
    diagnostics = {
        "finish_reason": finish_reason,
        "reasoning_chars": len(reasoning) if isinstance(reasoning, str) else 0,
        "completion_tokens": getattr(usage, "completion_tokens", None),
        "raw": _first_message_text(response),
        "truncated": finish_reason == "length",
    }
    return diagnostics["raw"], diagnostics


def _describe_reply(diagnostics: dict[str, Any]) -> str:
    """Summarize why a reply was unusable."""
    parts = [f"finish_reason={diagnostics.get('finish_reason') or '未知'}"]
    if diagnostics.get("reasoning_chars"):
        parts.append(f"推理内容 {diagnostics['reasoning_chars']} 字（不是行动）")
    if diagnostics.get("completion_tokens") is not None:
        parts.append(f"输出 {diagnostics['completion_tokens']} tokens")
    if diagnostics.get("truncated"):
        parts.append("输出被截断，建议提高 max_output_tokens 或换用非推理模型")
    return "，".join(parts)


def _first_message_text(response: Any) -> str:
    """Extract the assistant text from a chat completion response."""
    choices = getattr(response, "choices", None)
    if not choices:
        return ""
    message = getattr(choices[0], "message", None)
    if message is None:
        return ""
    content = getattr(message, "content", None)
    if isinstance(content, str):
        return content
    if isinstance(content, list):
        parts = [
            str(part.get("text", ""))
            for part in content
            if isinstance(part, dict) and part.get("type") in (None, "text")
        ]
        return " ".join(parts)
    return ""


# Fallbacks must survive a strict referee: concrete, executable, self-directed
# actions that name what the character physically does. "处理眼前的问题" style
# intent gets refused, and retrying the identical text would just be refused again.
FALLBACK_ACTIONS: tuple[str, ...] = (
    "我拿起最近的那件东西，仔细看一遍。",
    "我蹲下看看地上有没有痕迹。",
    "我走到门边，检查门锁和门框。",
    "我翻开手边最近的抽屉或柜子看看。",
    "我回到同伴身边，把看到的告诉他们。",
    "我站到能看清全局的位置，记下进出路径。",
)


def fallback_action(character: Character, round_number: int, attempt: int = 1) -> str:
    """Return a concrete fallback action, varied by round and attempt.

    It reads like player input: first person, no character-name prefix. A
    "邱明走过去检查炉子。" style third-person line is what a DM writes, and a
    card author reading the report would rightly say the seat does not sound
    like a player at all.
    """
    size = len(FALLBACK_ACTIONS)
    index = max(1, round_number) - 1 + attempt - 1 + stable_offset(character.name, size)
    return FALLBACK_ACTIONS[index % size]

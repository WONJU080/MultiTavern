"""Pre-flight checks for a real (paid) playtest run.

``playtest validate`` proves the card is playable; this module proves the run
can actually start: the server answers, the room's DM credential is usable and
every seat can reach its endpoint with a model it names. Every check is
injectable so the suite never touches the network.
"""

from __future__ import annotations

import asyncio
import json
import ssl
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Awaitable, Callable, Sequence

from playtest.card import CardReport
from playtest.agent import use_system_proxy
from playtest.config import PlaytestConfig, SeatPlan, plan_seats

CHECK_TIMEOUT_SECONDS = 10.0
LEVEL_MARKS = {"ok": "✓", "warn": "⚠", "fail": "✗"}

# A probe answers (http_status, detail). Status 0 means "could not connect".
ProbeResult = tuple[int, str]
Probe = Callable[[str, str | None, bool, bool], Awaitable[ProbeResult]]


@dataclass(frozen=True, slots=True)
class CheckResult:
    """One pre-flight result."""

    name: str
    level: str
    detail: str = ""

    @property
    def failed(self) -> bool:
        """Return whether this result blocks a run."""
        return self.level == "fail"

    def as_dict(self) -> dict[str, str]:
        """Return a JSON-serializable representation."""
        return {"name": self.name, "level": self.level, "detail": self.detail}


async def http_probe(
    url: str, api_key: str | None, verify_tls: bool, use_proxy: bool = False
) -> ProbeResult:
    """Fetch one URL and report its status without raising.

    Uses the standard library: a driver environment can carry a broken or
    shadowed HTTP client, and a pre-flight check must still tell the truth.
    """
    return await asyncio.to_thread(_probe_sync, url, api_key, verify_tls, use_proxy)


def _probe_sync(
    url: str, api_key: str | None, verify_tls: bool, use_proxy: bool = False
) -> ProbeResult:
    """Blocking GET used by :func:`http_probe`."""
    headers = {"Authorization": f"Bearer {api_key}"} if api_key else {}
    request = urllib.request.Request(url, headers=headers)
    handlers: list[Any] = []
    # Match the run path: a self-hosted server is reached directly unless the
    # configuration opts into the system proxy.
    if not use_proxy:
        handlers.append(urllib.request.ProxyHandler({}))
    if url.startswith("https://"):
        context = ssl.create_default_context()
        if not verify_tls:
            context.check_hostname = False
            context.verify_mode = ssl.CERT_NONE
        handlers.append(urllib.request.HTTPSHandler(context=context))
    opener = urllib.request.build_opener(*handlers)
    try:
        with opener.open(request, timeout=CHECK_TIMEOUT_SECONDS) as body:
            return body.status, _summarize(body.status, body.read(65_536))
    except urllib.error.HTTPError as exc:
        return exc.code, f"HTTP {exc.code}"
    except (urllib.error.URLError, OSError, ssl.SSLError) as exc:
        return 0, f"无法连接：{describe_error(exc)}"


def describe_error(exc: BaseException) -> str:
    """Render a connection error, whose str() is often empty."""
    message = str(exc).strip()
    if message:
        return message
    cause = exc.__cause__
    if cause is not None:
        return f"{type(exc).__name__}（{cause}）"
    return type(exc).__name__


def _summarize(status: int, body: bytes) -> str:
    """Return a short, non-secret description of a response body."""
    if status != 200:
        return f"HTTP {status}"
    try:
        payload = json.loads(body.decode("utf-8", errors="replace"))
    except ValueError:
        return "200"
    if isinstance(payload, dict) and isinstance(payload.get("data"), list):
        names = [
            str(item.get("id"))
            for item in payload["data"]
            if isinstance(item, dict) and item.get("id")
        ]
        return f"200，模型 {len(names)} 个：" + "、".join(names[:8])
    return "200"


def model_names(status: int, detail: str) -> list[str]:
    """Extract model ids from a probe detail string."""
    if status != 200 or "模型" not in detail:
        return []
    _, _, listed = detail.partition("：")
    return [item for item in listed.split("、") if item]


async def check_server(config: PlaytestConfig, probe: Probe) -> CheckResult:
    """Check that the Anyworld server answers its index page."""
    status, detail = await probe(
        f"{config.server.url}/", None, config.server.verify_tls, config.server.proxy
    )
    if status == 0:
        return CheckResult("服务器连通", "fail", detail)
    if status == 200:
        return CheckResult("服务器连通", "ok", config.server.url)
    return CheckResult("服务器连通", "fail", f"{config.server.url} 返回 {detail}")


async def check_dm(config: PlaytestConfig, probe: Probe) -> CheckResult:
    """Check the room's DM credential."""
    if config.dm.mode == "borrow":
        return CheckResult(
            "DM 凭据",
            "warn",
            "borrow：房间将借用服务器 llm.api_key（受 borrowed_room_limit 限制），离线无法验证。",
        )
    if not config.dm.endpoint:
        return CheckResult("DM 凭据", "fail", "dm.mode=own 但未配置 endpoint。")
    key = config.dm.resolved_api_key()
    if not key:
        return CheckResult("DM 凭据", "fail", "dm.mode=own 但没有可用的 API Key。")
    status, detail = await probe(
        f"{config.dm.endpoint.rstrip('/')}/models",
        key,
        config.server.verify_tls,
        config.server.proxy,
    )
    if status == 0:
        return CheckResult("DM 凭据", "fail", f"DM 端点不可达：{detail}")
    if status in (401, 403):
        return CheckResult("DM 凭据", "fail", f"DM Key 被拒绝（{detail}）。")
    if config.dm.model_name and model_names(status, detail):
        if config.dm.model_name not in model_names(status, detail):
            return CheckResult(
                "DM 凭据",
                "warn",
                f"端点可用，但模型列表里没有 {config.dm.model_name}（{detail}）。",
            )
    return CheckResult("DM 凭据", "ok", f"{config.dm.endpoint}（{detail}）")


async def check_seats(
    config: PlaytestConfig, plans: Sequence[SeatPlan], probe: Probe
) -> list[CheckResult]:
    """Check every distinct seat endpoint and its model."""
    results: list[CheckResult] = []
    seen: set[tuple[str, str]] = set()
    for plan in plans:
        seat = plan.llm
        if config.run.dry_run:
            results.append(
                CheckResult(
                    f"座位 {plan.character}",
                    "warn",
                    "dry-run：座位使用脚本化行动，不会调用 LLM。",
                )
            )
            continue
        if not seat.endpoint or not seat.model_name:
            results.append(
                CheckResult(
                    f"座位 {plan.character}",
                    "fail",
                    "缺少 endpoint 或 model_name。",
                )
            )
            continue
        signature = (seat.endpoint, seat.model_name)
        if signature in seen:
            continue
        seen.add(signature)
        key = seat.resolved_api_key()
        if not key:
            results.append(
                CheckResult(
                    f"座位 {plan.character}",
                    "fail",
                    f"{seat.endpoint} 没有可用的 API Key（检查 api_key_env 是否已导出）。",
                )
            )
            continue
        status, detail = await probe(
            f"{seat.endpoint.rstrip('/')}/models",
            key,
            config.server.verify_tls,
            use_system_proxy(seat),
        )
        if status == 0:
            results.append(CheckResult(f"座位 {plan.character}", "fail", detail))
            continue
        if status in (401, 403):
            results.append(
                CheckResult(f"座位 {plan.character}", "fail", f"Key 被拒绝（{detail}）。")
            )
            continue
        listed = model_names(status, detail)
        if listed and seat.model_name not in listed:
            results.append(
                CheckResult(
                    f"座位 {plan.character}",
                    "warn",
                    f"{seat.endpoint} 可用，但模型列表里没有 {seat.model_name}：{detail}",
                )
            )
            continue
        results.append(
            CheckResult(
                f"座位 {plan.character}",
                "ok",
                f"{seat.endpoint} / {seat.model_name}（{detail}）",
            )
        )
    return results


async def run_checks(
    config: PlaytestConfig, card: CardReport, *, probe: Probe | None = None
) -> list[CheckResult]:
    """Run every pre-flight check for one card and configuration."""
    probe = probe or http_probe
    results: list[CheckResult] = []
    if card.ok and card.setup is not None:
        matches = not config.card or config.card == str(card.path)
        results.append(
            CheckResult(
                "剧情卡",
                "ok" if matches else "warn",
                f"《{card.title}》{card.schedule} 模式，{len(card.setup.cast)} 个角色"
                + ("" if matches else f"；配置里的 card 指向 {config.card}"),
            )
        )
    else:
        results.append(
            CheckResult(
                "剧情卡",
                "fail",
                "未通过校验：" + "；".join(item.message for item in card.errors[:3]),
            )
        )
        return results
    results.append(await check_server(config, probe))
    results.append(await check_dm(config, probe))
    plans = plan_seats(
        config.seats,
        [character.name for character in card.setup.cast],
        host_character=card.setup.host_character or None,
    )
    results.extend(await check_seats(config, plans, probe))
    return results


def summarize(results: Sequence[CheckResult]) -> tuple[int, int, int]:
    """Return (ok, warn, fail) counts."""
    return (
        sum(1 for item in results if item.level == "ok"),
        sum(1 for item in results if item.level == "warn"),
        sum(1 for item in results if item.level == "fail"),
    )


def run_checks_sync(
    config: PlaytestConfig, card: CardReport, *, probe: Probe | None = None
) -> list[CheckResult]:
    """Blocking wrapper used by the CLI."""
    return asyncio.run(run_checks(config, card, probe=probe))


def describe(result: CheckResult) -> str:
    """Render one check result for a terminal."""
    mark = LEVEL_MARKS.get(result.level, "-")
    detail = f"：{result.detail}" if result.detail else ""
    return f"  {mark} {result.name}{detail}"

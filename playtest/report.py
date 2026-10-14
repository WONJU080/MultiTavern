"""Run reports: what happened, what it cost, and how the card behaved.

M2 writes the run-level facts (rounds, actions, seats, token usage, stop
reason). Card-coverage metrics and health verdicts are layered on in M4; the
report structure already reserves their sections so old reports stay readable.
"""

from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

from playtest.coverage import summarize_coverage

SCHEMA_VERSION = 1
_SLUG_PATTERN = re.compile(r"[^0-9A-Za-z\u4e00-\u9fff]+")


def slugify(text: str, *, fallback: str = "card", limit: int = 40) -> str:
    """Return a filesystem-safe short name for a report directory."""
    cleaned = _SLUG_PATTERN.sub("-", text).strip("-")
    return (cleaned or fallback)[:limit]


@dataclass
class RunReport:
    """Everything one playtest run produced."""

    card_path: str
    card_digest: str
    card_title: str
    schedule: str
    server_url: str
    dm_mode: str
    room_code: str = ""
    started_at: str = ""
    finished_at: str = ""
    elapsed_seconds: float = 0.0
    stop_reason: str = ""
    dry_run: bool = False
    rounds: list[dict[str, Any]] = field(default_factory=list)
    seats: list[dict[str, Any]] = field(default_factory=list)
    token_usage: list[dict[str, Any]] = field(default_factory=list)
    errors: list[str] = field(default_factory=list)
    rejections: list[str] = field(default_factory=list)
    pauses: list[str] = field(default_factory=list)
    warnings: list[str] = field(default_factory=list)
    notices: list[str] = field(default_factory=list)
    coverage: dict[str, Any] = field(default_factory=dict)
    health: dict[str, Any] = field(default_factory=dict)

    @property
    def duration_seconds(self) -> float:
        """Return the wall-clock duration of the run."""
        if self.elapsed_seconds:
            return self.elapsed_seconds
        if not self.started_at or not self.finished_at:
            return 0.0
        try:
            start = datetime.fromisoformat(self.started_at)
            end = datetime.fromisoformat(self.finished_at)
        except ValueError:
            return 0.0
        return max(0.0, (end - start).total_seconds())

    @property
    def final_game_tokens(self) -> int | None:
        """Return the last reported cumulative game token total."""
        for snapshot in reversed(self.token_usage):
            game = snapshot.get("game")
            if isinstance(game, dict) and isinstance(game.get("total_tokens"), int):
                return int(game["total_tokens"])
        return None

    @property
    def rounds_completed(self) -> int:
        """Return how many rounds resolved."""
        return sum(1 for item in self.rounds if item.get("narrative"))

    def as_dict(self) -> dict[str, Any]:
        """Return a JSON-serializable representation."""
        return {
            "schema_version": SCHEMA_VERSION,
            "card": {
                "path": self.card_path,
                "digest": self.card_digest,
                "title": self.card_title,
                "schedule": self.schedule,
            },
            "run": {
                "server_url": self.server_url,
                "dm_mode": self.dm_mode,
                "room_code": self.room_code,
                "started_at": self.started_at,
                "finished_at": self.finished_at,
                "duration_seconds": round(self.duration_seconds, 2),
                "stop_reason": self.stop_reason,
                "dry_run": self.dry_run,
                "rounds_completed": self.rounds_completed,
                "game_tokens": self.final_game_tokens,
            },
            "seats": self.seats,
            "rounds": self.rounds,
            "token_usage": self.token_usage,
            "errors": self.errors,
            "rejections": self.rejections,
            "pauses": self.pauses,
            "warnings": self.warnings,
            "notices": self.notices,
            "coverage": self.coverage,
            "health": self.health,
        }

    def to_json(self) -> str:
        """Serialize the report as pretty JSON."""
        return json.dumps(self.as_dict(), ensure_ascii=False, indent=2)

    def summary_line(self) -> str:
        """Return a one-line human summary."""
        tokens = self.final_game_tokens
        token_text = f"，DM token ≈ {tokens}" if tokens is not None else ""
        verdict = self.verdict
        verdict_text = {"ok": "正常结束", "unfinished": "未达结局", "failed": "运行失败"}.get(
            verdict, verdict
        )
        coverage = summarize_coverage(self.coverage) if self.coverage else ""
        coverage_text = f"；覆盖：{coverage}" if coverage else ""
        return (
            f"《{self.card_title}》{self.schedule} 模式：完成 {self.rounds_completed} 回合，"
            f"耗时 {self.duration_seconds:.0f}s{token_text}；结论：{verdict_text}"
            f"（{self.stop_reason}）{coverage_text}"
        )

    @property
    def verdict(self) -> str:
        """Return the run's health verdict."""
        return str(self.health.get("verdict") or "ok")

    @property
    def exit_code(self) -> int:
        """Return the process exit code this run deserves."""
        return {"ok": 0, "unfinished": 3, "failed": 2}.get(self.verdict, 0)

    def to_markdown(self) -> str:
        """Render the report as Markdown."""
        lines = [
            f"# 跑测报告：《{self.card_title}》",
            "",
            f"- 卡牌：`{self.card_path}`（sha256 `{self.card_digest[:12]}`）",
            f"- 调度模式：{self.schedule}",
            f"- 服务器：{self.server_url}（DM 凭据：{self.dm_mode}）",
            f"- 房间码：{self.room_code or '—'}",
            f"- 起止：{self.started_at} → {self.finished_at}（{self.duration_seconds:.0f}s）",
            f"- 完成回合：{self.rounds_completed}；停止原因：{self.stop_reason}",
            f"- 结论：{self.verdict}（退出码 {self.exit_code}）",
        ]
        if self.final_game_tokens is not None:
            lines.append(f"- DM 累计 token：{self.final_game_tokens}")
        if self.dry_run:
            lines.append("- 座位模式：脚本化（--dry-run，未调用座位 LLM）")

        if self.coverage:
            lines += self._coverage_markdown()
        if self.health:
            lines += self._health_markdown()

        lines += [
            "",
            "## 座位",
            "",
            "| 角色 | 风格 | 模型 | 调用 | 失败 | 重试 | 兜底 | 强制 | 真相 | 行动 |",
            "| --- | --- | --- | --- | --- | --- | --- | --- | --- | --- |",
        ]
        for seat in self.seats:
            lines.append(
                "| {character} | {style} | {model} | {calls} | {failures} | {retries} | "
                "{fallbacks} | {forced} | {truth} | {actions} |".format(**seat)
            )

        lines += ["", "## 回合时间线", ""]
        if not self.rounds:
            lines.append("（没有完成的回合）")
        for record in self.rounds:
            heading = f"### 第 {record.get('number')} 回合"
            if record.get("game_time"):
                heading += f"（{record['game_time']}）"
            lines.append(heading)
            for character, action in (record.get("actions") or {}).items():
                lines.append(f"- **{character}**：{action}")
            narrative = record.get("narrative")
            if narrative:
                lines += ["", f"> {narrative}", ""]
            for character, outcome in (record.get("outcomes") or {}).items():
                lines.append(f"- *{character}*：{outcome}")
            dice = record.get("dice") or {}
            if dice:
                lines.append("- 骰点：" + "，".join(f"{k}={v}" for k, v in dice.items()))
            lines.append("")

        if self.warnings or self.errors or self.rejections:
            lines += ["## 告警与错误", ""]
            lines += [f"- ⚠ {item}" for item in self.warnings]
            lines += [f"- ℹ 提交被驳回（已自动重试）：{item}" for item in self.rejections[:10]]
            lines += [f"- ✗ {item}" for item in self.errors]
            lines.append("")
        if self.notices:
            lines += ["## 系统消息", ""]
            lines += [f"- {item}" for item in self.notices]
            lines.append("")
        return "\n".join(lines).rstrip() + "\n"

    def _coverage_markdown(self) -> list[str]:
        """Render the coverage section."""
        regions = self.coverage.get("regions") or {}
        items = self.coverage.get("items") or {}
        events = self.coverage.get("events") or {}
        lorebook = self.coverage.get("lorebook") or {}
        truths = self.coverage.get("truths") or {}
        time_info = self.coverage.get("time") or {}
        lines = [
            "",
            "## 覆盖度",
            "",
            f"- 地区：访问 {regions.get('visited', 0)}/{regions.get('total', 0)}"
            + (
                f"，未访问：{'、'.join(regions.get('unvisited_names') or [])}"
                if regions.get("unvisited_names")
                else ""
            ),
            f"- 物品：发现 {items.get('discovered', 0)}/{items.get('total', 0)}，"
            f"已调查 {items.get('investigated', 0)}，携带 {len(items.get('carried_names') or [])}"
            + (
                f"；线索物品 发现 {items.get('clue_discovered', 0)}/{items.get('clue_total', 0)}"
                f"（调查 {items.get('clue_investigated', 0)}）"
                if items.get("clue_total")
                else ""
            ),
            f"- 事件：触发 {events.get('fired', 0)}/{events.get('total', 0)}"
            + (
                f"，已过时未触发：{'、'.join(events.get('overdue') or [])}"
                if events.get("overdue")
                else ""
            ),
            f"- 世界书：关键词命中 {lorebook.get('hit', 0)}/{lorebook.get('total', 0)}（启发式，服务器不公开实际注入）",
            f"- 真相：揭晓 {truths.get('revealed', 0)}/{truths.get('total', 0)}，"
            f"有可判定陈述 {truths.get('answered', 0)}",
            f"- 结算边界时间：{time_info.get('start') or '—'} → {time_info.get('end') or '—'}"
            f"（推进 {time_info.get('elapsed_minutes', 0)} 分钟 / "
            f"{time_info.get('boundaries', 0)} 个结算边界）",
            "",
            "| 角色 | 有效行动回合 | 被叙事提及 | 最长空档 |",
            "| --- | --- | --- | --- |",
        ]
        for row in self.coverage.get("participation") or []:
            lines.append(
                f"| {row.get('character')} | {row.get('action_rounds')} | "
                f"{row.get('mentioned_rounds')} | {row.get('longest_gap')} |"
            )
        if truths.get("claims"):
            lines += ["", "已成立的陈述："]
            lines += [
                f"- [{claim.get('question_id')}] {claim.get('character')}：{claim.get('text')}"
                for claim in truths["claims"]
            ]
        lines.append("")
        return lines

    def _health_markdown(self) -> list[str]:
        """Render the health section."""
        lines = ["", "## 健康检查", "", f"- 结论：{self.verdict}（退出码 {self.exit_code}）"]
        issues = self.health.get("issues") or []
        if not issues:
            lines.append("- 未发现异常。")
        for issue in issues:
            mark = {"error": "✗", "warning": "⚠", "info": "ℹ"}.get(issue.get("level"), "-")
            lines.append(f"- {mark} [{issue.get('code')}] {issue.get('message')}")
        lines.append("")
        return lines


def run_directory(out_dir: str | Path, card_title: str, *, now: datetime | None = None) -> Path:
    """Return the timestamped directory one run writes into."""
    stamp = (now or datetime.now()).strftime("%Y%m%d-%H%M%S")
    return Path(out_dir) / f"{stamp}-{slugify(card_title)}"


def report_directory(
    out_dir: str | Path, report: RunReport, *, now: datetime | None = None
) -> Path:
    """Return the timestamped directory one run's report writes into."""
    return run_directory(out_dir, report.card_title, now=now)


def write_report(
    report: RunReport,
    out_dir: str | Path,
    formats: list[str] | None = None,
    *,
    now: datetime | None = None,
    directory: Path | None = None,
) -> list[Path]:
    """Write the requested report formats and return the written paths."""
    wanted = formats or ["json"]
    directory = directory or report_directory(out_dir, report, now=now)
    directory.mkdir(parents=True, exist_ok=True)
    written: list[Path] = []
    if "json" in wanted:
        path = directory / "run.json"
        path.write_text(report.to_json() + "\n", encoding="utf-8")
        written.append(path)
    if "markdown" in wanted:
        path = directory / "report.md"
        path.write_text(report.to_markdown(), encoding="utf-8")
        written.append(path)
    return written

"""Command-line entry point for the playtest driver.

``validate`` checks a card offline with the server's own validation plus static
design checks. ``run`` drives a real game against a running Anyworld server:
one host client plus one AI seat per character, then writes a run report.
"""

from __future__ import annotations

import argparse
import asyncio
import json
import sys
from pathlib import Path
from typing import Any, Sequence

from playtest.card import CardFileError, CardReport, Finding, discover_cards, inspect_card
from playtest.config import PlaytestConfig, load_config, plan_seats
from playtest.report import write_report

EXIT_OK = 0
EXIT_INVALID = 1
EXIT_USAGE = 2
EXIT_WARNINGS = 3
EXIT_UNFINISHED = 3

LEVEL_MARKS = {"error": "✗", "warning": "⚠", "info": "ℹ"}
SCHEDULE_LABELS = {"round": "round（轮流交卷）", "strict": "strict（时间轴）"}


def build_parser() -> argparse.ArgumentParser:
    """Build the top-level argument parser."""
    parser = argparse.ArgumentParser(
        prog="playtest",
        description="剧情卡自动跑测工具：离线校验与多座位 AI 试玩。",
        epilog=(
            "退出码：0 通过/正常结束；1 校验失败或跑测失败；2 用法或配置错误；"
            "3 仅有告警（--strict）或撞到预算上限未达结局。"
        ),
    )
    subparsers = parser.add_subparsers(dest="command", required=True)

    validate = subparsers.add_parser(
        "validate",
        help="校验剧情卡（不联网、不消耗 token）",
        description=(
            "用服务器自身的校验逻辑加静态设计检查来预检剧情卡："
            "能被服务器拒收的问题、以及线索不可达、事件永不触发之类的设计漏洞。"
        ),
    )
    validate.add_argument("--card", action="append", default=[], help="剧情卡 JSON 路径，可重复")
    validate.add_argument(
        "--cards", action="append", default=[], help="包含 *.anyworld.json 的目录，可重复"
    )
    validate.add_argument("--config", help="跑测配置 YAML；给出时会一并交叉检查座位配置")
    validate.add_argument("--json", action="store_true", help="以 JSON 输出，便于脚本化")
    validate.add_argument("--strict", action="store_true", help="把告警视为失败（退出码 3）")
    validate.add_argument("--quiet", action="store_true", help="只输出失败项与汇总")

    run = subparsers.add_parser(
        "run",
        help="连接运行中的服务器，用 AI 座位真跑一遍剧情卡",
        description=(
            "以真实 WebSocket 客户端跑测：房主建房并注入剧情卡，每个角色一个 AI 座位，"
            "轮到谁就调用该座位的 LLM 提交行动，结束后写出 run.json / report.md。"
        ),
    )
    run.add_argument("--config", required=True, help="跑测配置 YAML（座位、预算、服务器地址）")
    run.add_argument("--card", help="覆盖配置里的剧情卡路径")
    run.add_argument("--url", help="覆盖配置里的服务器地址")
    run.add_argument("--max-rounds", type=int, help="覆盖配置里的回合上限")
    run.add_argument("--dry-run", action="store_true", help="座位改用脚本化行动，不调用座位 LLM")
    run.add_argument(
        "--debug-prompts",
        action="store_true",
        help="把每个座位收到的完整提示词写入报告目录的 prompts/（排查不像玩家的问题）",
    )
    run.add_argument("--out-dir", help="覆盖报告输出目录")
    run.add_argument("--quiet", action="store_true", help="不打印实时进度，只在结束时给摘要")
    run.add_argument("--json", action="store_true", help="把运行报告 JSON 也打到 stdout")

    check = subparsers.add_parser(
        "check",
        help="真实跑测前的连通性自检（服务器 / DM 凭据 / 座位端点）",
        description=(
            "在花钱跑测之前确认：服务器能连上、DM 凭据可用、每个座位的端点与模型都可达。"
            "只发少量 GET 请求，不消耗生成 token。"
        ),
    )
    check.add_argument("--config", required=True, help="跑测配置 YAML")
    check.add_argument("--card", help="覆盖配置里的剧情卡路径")
    check.add_argument("--json", action="store_true", help="以 JSON 输出")

    serve = subparsers.add_parser(
        "serve",
        help="打开本地跑测台网页（填 API、自检、跑测、看报告）",
        description=(
            "在本机启动一个单页跑测台：选择剧情卡、填入服务器与座位 API、先自检再跑测，"
            "实时看进度与最终报告。默认只监听 127.0.0.1，API Key 只保存在该进程内存中。"
        ),
    )
    serve.add_argument("--host", default="127.0.0.1", help="监听地址（默认仅本机）")
    serve.add_argument("--port", type=int, default=8787, help="监听端口（默认 8787）")
    serve.add_argument(
        "--cards",
        action="append",
        default=[],
        help="卡牌来源目录或文件，可重复（默认 examples）",
    )
    serve.add_argument("--no-open", action="store_true", help="不要自动打开浏览器")
    return parser


def _collect_paths(cards: Sequence[str], directories: Sequence[str]) -> list[Path]:
    """Return the card paths named directly or found in the given directories."""
    paths: list[Path] = [Path(item) for item in cards]
    for directory in directories:
        paths.extend(discover_cards(directory))
    unique: list[Path] = []
    seen: set[Path] = set()
    for path in paths:
        if path in seen:
            continue
        seen.add(path)
        unique.append(path)
    return unique


def config_findings(config: PlaytestConfig, report: CardReport) -> list[Finding]:
    """Cross-check a run configuration against one validated card."""
    findings: list[Finding] = []
    if report.setup is None:
        return findings
    setup = report.setup
    characters = [character.name for character in setup.cast]
    unknown = config.seats.unknown_overrides(characters)
    if unknown:
        findings.append(
            Finding(
                "warning",
                "unknown_seat_override",
                "seats.overrides 中的名字不在本卡角色名单中，会被忽略：" + "、".join(unknown),
            )
        )
    if config.card and Path(config.card).name != report.path.name:
        findings.append(
            Finding(
                "info",
                "config_card_mismatch",
                f"配置里的 card 指向 {config.card}，本次校验的是 {report.path.name}。",
            )
        )
    if not config.run.dry_run:
        incomplete = [
            plan.character
            for plan in plan_seats(config.seats, characters)
            if not plan.llm.is_complete()
        ]
        if incomplete:
            findings.append(
                Finding(
                    "warning",
                    "seat_incomplete",
                    "以下角色没有可用的座位 LLM（缺 endpoint 或 model_name）："
                    + "、".join(incomplete),
                )
            )
    if config.dm.mode == "own":
        if not config.dm.endpoint:
            findings.append(
                Finding("warning", "dm_endpoint_missing", "dm.mode=own 但未填写 dm.endpoint。")
            )
        key = config.dm.resolved_api_key()
        if not key:
            if config.dm.api_key_env:
                findings.append(
                    Finding(
                        "warning",
                        "dm_key_unset",
                        f"dm.api_key_env={config.dm.api_key_env} 在当前环境中未设置，"
                        "运行时房间会进入待补 Key 状态。",
                    )
                )
            else:
                findings.append(
                    Finding("warning", "dm_key_missing", "dm.mode=own 但没有可用的 API Key。")
                )
    else:
        findings.append(
            Finding(
                "info",
                "dm_borrow",
                "dm.mode=borrow：房间借用服务器的 llm.api_key，受 server.borrowed_room_limit 限制。",
            )
        )
    if setup.host_character:
        findings.append(
            Finding(
                "info",
                "host_character",
                f"卡里指定 host_character={setup.host_character}：房主连接自己驱动该角色，"
                "其余角色由座位驱动。",
            )
        )
    else:
        findings.append(
            Finding(
                "info",
                "host_observes",
                "卡里没有 host_character：房主只当房主，全部角色交给座位驱动。",
            )
        )
    return findings


def _describe(report: CardReport) -> str:
    """Render the one-line structural summary of a card."""
    counts = report.counts()
    if not counts:
        return ""
    return (
        f"      角色 {counts['characters']} · 地区 {counts['regions']} · "
        f"物品 {counts['items']}（线索 {counts['clue_items']}）· 事件 {counts['events']} · "
        f"世界书 {counts['lorebook']} · 提示块 {counts['prompt_blocks']} · "
        f"真相提问 {counts['truth_questions']}"
    )


def _print_report(report: CardReport, index: int, total: int, quiet: bool) -> None:
    """Print one card's human-readable block."""
    print(f"[{index}/{total}] {report.path}")
    schedule = SCHEDULE_LABELS.get(report.schedule or "", report.schedule or "未知")
    digest = report.digest[:12] or "-"
    print(f"      标题：{report.title}    调度：{schedule}    sha256 {digest}")
    summary = _describe(report)
    if summary:
        print(summary)
    if report.ok:
        print("      ✓ 通过服务器校验（logic.lobby.parse_scenario_setup）")
    for finding in report.findings:
        if quiet and finding.level != "error":
            continue
        print(f"      {LEVEL_MARKS.get(finding.level, '-')} [{finding.code}] {finding.message}")


def _validate(args: argparse.Namespace) -> int:
    """Run the validate sub-command and return its exit code."""
    try:
        paths = _collect_paths(args.card, args.cards)
    except CardFileError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE
    if not paths:
        print("错误：至少需要 --card 或 --cards 指定要校验的剧情卡。", file=sys.stderr)
        return EXIT_USAGE

    config: PlaytestConfig | None = None
    if args.config:
        try:
            config = load_config(args.config)
        except ValueError as exc:
            print(f"错误：{exc}", file=sys.stderr)
            return EXIT_USAGE

    reports: list[CardReport] = []
    for path in paths:
        report = inspect_card(path)
        if config is not None:
            extra = config_findings(config, report)
            if extra:
                report = CardReport(
                    path=report.path,
                    digest=report.digest,
                    payload=report.payload,
                    setup=report.setup,
                    findings=report.findings + tuple(extra),
                )
        reports.append(report)

    failed = sum(1 for report in reports if not report.ok)
    warnings = sum(len(report.warnings) for report in reports)
    infos = sum(len(report.infos) for report in reports)

    if args.json:
        payload: dict[str, Any] = {
            "ok": failed == 0 and not (args.strict and warnings),
            "config": args.config or None,
            "cards": [report.as_dict() for report in reports],
            "summary": {
                "total": len(reports),
                "passed": len(reports) - failed,
                "failed": failed,
                "warnings": warnings,
                "infos": infos,
            },
        }
        print(json.dumps(payload, ensure_ascii=False, indent=2))
    else:
        for index, report in enumerate(reports, start=1):
            _print_report(report, index, len(reports), args.quiet)
        print(
            f"汇总：{len(reports)} 张卡，通过 {len(reports) - failed}，失败 {failed}，"
            f"告警 {warnings}，提示 {infos}。"
        )

    if failed:
        return EXIT_INVALID
    if args.strict and warnings:
        return EXIT_WARNINGS
    return EXIT_OK


def _apply_run_overrides(config: PlaytestConfig, args: argparse.Namespace) -> PlaytestConfig:
    """Apply CLI overrides on top of the loaded configuration."""
    server = config.server
    if args.url:
        server = server.model_copy(update={"url": args.url.rstrip("/")})
    limits = config.run
    updates: dict[str, Any] = {}
    if args.max_rounds is not None:
        if args.max_rounds < 1:
            raise ValueError("--max-rounds 必须大于 0。")
        updates["max_rounds"] = args.max_rounds
    if args.dry_run:
        updates["dry_run"] = True
    if args.debug_prompts:
        updates["debug_prompts"] = True
    if updates:
        limits = limits.model_copy(update=updates)
    return config.model_copy(update={"server": server, "run": limits})


def _print_seat_plan(card: CardReport, config: PlaytestConfig) -> None:
    """Print the seat line-up before a run starts."""
    if card.setup is None:
        return
    plans = plan_seats(
        config.seats,
        [character.name for character in card.setup.cast],
        host_character=card.setup.host_character or None,
    )
    mode = "脚本化（dry-run）" if config.run.dry_run else "LLM"
    print(f"座位安排（{mode}）：")
    for plan in plans:
        role = "房主连接" if plan.host_seat else "独立连接"
        if config.run.dry_run:
            print(f"  · {plan.character}｜{plan.style}｜{role}")
            continue
        endpoint = plan.llm.endpoint or "（未配置 endpoint）"
        model = plan.llm.model_name or "（未配置模型）"
        print(f"  · {plan.character}｜{plan.style}｜{model}｜{endpoint}｜{role}")


def _run(args: argparse.Namespace) -> int:
    """Run one playtest against a live server and write the report."""
    from playtest.runner import PlaytestRunner

    try:
        config = load_config(args.config)
        config = _apply_run_overrides(config, args)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE

    card_path = args.card or config.card
    if not card_path:
        print("错误：请在配置里写 card，或用 --card 指定剧情卡。", file=sys.stderr)
        return EXIT_USAGE
    card = inspect_card(card_path)
    if not card.ok:
        print(f"错误：剧情卡未通过校验：{card_path}", file=sys.stderr)
        for finding in card.errors:
            print(f"  ✗ [{finding.code}] {finding.message}", file=sys.stderr)
        print(f"提示：先运行 python -m playtest validate --card {card_path}", file=sys.stderr)
        return EXIT_INVALID

    print(f"剧情卡：《{card.title}》（{card.schedule} 模式，sha256 {card.digest[:12]}）")
    print(f"服务器：{config.server.url}（DM 凭据：{config.dm.mode}）")
    _print_seat_plan(card, config)
    print(f"预算：最多 {config.run.max_rounds} 回合 / {config.run.max_minutes:.0f} 分钟")

    # Stream progress: a real run takes minutes and a blank terminal tells
    # the operator nothing about whether the seats are actually playing.
    live = not args.quiet

    def on_progress(event: dict) -> None:
        if not live:
            return
        elapsed = event.get("elapsed") or 0.0
        print(f"  [{elapsed:5.1f}s] {event.get('kind', '?'):<12} {event.get('message', '')}")

    runner = PlaytestRunner(config, card, progress=on_progress)
    try:
        report = asyncio.run(runner.run())
    except KeyboardInterrupt:
        print("已中断；房间可能仍存在于服务器上。", file=sys.stderr)
        return EXIT_USAGE

    out_dir = args.out_dir or config.report.out_dir
    # Prompts are dumped into the run directory while it runs; the report joins them.
    written = write_report(report, out_dir, config.report.formats, directory=runner.run_dir)
    print(report.summary_line())
    for path in written:
        print(f"报告：{path}")
    issues = (report.health or {}).get("issues") or []
    if issues:
        print(f"健康检查 {len(issues)} 条：")
        marks = {"error": "✗", "warning": "⚠", "info": "ℹ"}
        for issue in issues[:12]:
            mark = marks.get(str(issue.get("level")), "-")
            print(f"  {mark} [{issue.get('code')}] {issue.get('message')}")
    elif report.warnings:
        print(f"告警 {len(report.warnings)} 条：")
        for item in report.warnings[:10]:
            print(f"  ⚠ {item}")
    if report.errors:
        print(f"错误 {len(report.errors)} 条：", file=sys.stderr)
        for item in report.errors[:10]:
            print(f"  ✗ {item}", file=sys.stderr)
    if args.json:
        print(report.to_json())

    if report.errors:
        return EXIT_INVALID
    # The report grades the run: 0 natural end, 2 failed, 3 unfinished.
    return report.exit_code


def _check(args: argparse.Namespace) -> int:
    """Run the pre-flight checks and report them."""
    from playtest.doctor import describe, run_checks_sync, summarize

    try:
        config = load_config(args.config)
    except ValueError as exc:
        print(f"错误：{exc}", file=sys.stderr)
        return EXIT_USAGE
    card_path = args.card or config.card
    if not card_path:
        print("错误：请在配置里写 card，或用 --card 指定剧情卡。", file=sys.stderr)
        return EXIT_USAGE
    card = inspect_card(card_path)
    results = run_checks_sync(config, card)
    ok, warn, failed = summarize(results)

    if args.json:
        print(
            json.dumps(
                {
                    "ok": failed == 0,
                    "summary": {"ok": ok, "warn": warn, "fail": failed},
                    "checks": [item.as_dict() for item in results],
                },
                ensure_ascii=False,
                indent=2,
            )
        )
    else:
        print(f"跑测自检：{card_path}")
        for result in results:
            print(describe(result))
        print(f"汇总：通过 {ok}，提醒 {warn}，失败 {failed}。")
        if failed == 0:
            if warn:
                print(f"注意：{warn} 条提醒不阻止启动，但可能影响跑测结果。")
            print("可以开始真实跑测：python -m playtest run --config " + str(args.config))
    return EXIT_INVALID if failed else EXIT_OK


def _serve(args: argparse.Namespace) -> int:
    """Serve the local playtest UI."""
    import webbrowser

    from playtest.webapp import serve as serve_app

    cards = args.cards or ["examples"]
    url = f"http://{args.host}:{args.port}/"
    print("Anyworld 跑测台已启动：")
    print(f"  地址：{url}")
    print(f"  卡牌目录：{', '.join(cards)}")
    print("  提示：API Key 只存在这个进程内存里；按 Ctrl+C 结束。")
    if not args.no_open:
        try:
            webbrowser.open(url)
        except Exception:  # noqa: BLE001 - opening a browser is a convenience
            pass
    try:
        asyncio.run(serve_app(host=args.host, port=args.port, cards_dirs=cards))
    except KeyboardInterrupt:
        print("\n已停止。")
    return EXIT_OK


def main(argv: Sequence[str] | None = None) -> int:
    """Parse arguments and dispatch the requested sub-command."""
    parser = build_parser()
    args = parser.parse_args(list(argv) if argv is not None else None)
    if args.command == "validate":
        return _validate(args)
    if args.command == "run":
        return _run(args)
    if args.command == "check":
        return _check(args)
    if args.command == "serve":
        return _serve(args)
    parser.error(f"未知子命令 {args.command}")
    return EXIT_USAGE


if __name__ == "__main__":  # pragma: no cover - exercised through __main__
    raise SystemExit(main())

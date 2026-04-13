from __future__ import annotations

"""命令行入口，负责把用户输入组装成规则、持仓源和执行器。"""

import argparse
import json
import sys
import time
from decimal import Decimal
from typing import Sequence

from poly_shield.config import PolymarketCredentials, load_env_file
from poly_shield.executor import ExitExecutor
from poly_shield.polymarket import PolymarketGateway
from poly_shield.positions import GatewayPositionProvider, ManualPositionProvider
from poly_shield.quotes import OrderBookLevel
from poly_shield.rules import ExitRule, RuleKind, ZERO
from poly_shield.watcher import WatchTask, Watcher


def _decimal(value: str) -> Decimal:
    """把命令行字符串统一转换成 Decimal。"""
    return Decimal(value)


def build_parser() -> argparse.ArgumentParser:
    """构建 CLI 解析器，并为各子命令注册中文帮助信息。"""
    parser = argparse.ArgumentParser(
        prog="poly-shield", description="Polymarket 自动止盈止损命令行工具")
    subparsers = parser.add_subparsers(dest="command", required=True)

    positions_parser = subparsers.add_parser(
        "positions", help="从 Polymarket 官方持仓接口读取当前仓位")
    positions_parser.add_argument(
        "--token-id", help="可选，只查看指定的 Polymarket token ID")
    positions_parser.add_argument(
        "--size-threshold", type=_decimal, default=ZERO, help="最小持仓数量阈值，小于该值的仓位会被过滤")
    positions_parser.set_defaults(handler=handle_positions)

    watch_parser = subparsers.add_parser(
        "watch", help="持续监控单个 token，并在满足条件时执行止盈止损规则")
    watch_parser.add_argument(
        "--token-id", required=True, help="要监控的 Polymarket token ID")
    watch_parser.add_argument("--average-cost", type=_decimal,
                              help="手动指定持仓均价；保本止损未走自动持仓时需要提供")
    watch_parser.add_argument(
        "--position-size", type=_decimal, help="手动覆盖持仓数量")
    watch_parser.add_argument(
        "--breakeven-stop-ratio", type=_decimal, help="保本止损触发后的卖出比例")
    watch_parser.add_argument(
        "--price-stop", type=_decimal, help="固定价格止损触发价")
    watch_parser.add_argument(
        "--price-stop-ratio", type=_decimal, help="固定价格止损触发后的卖出比例")
    watch_parser.add_argument(
        "--take-profit", type=_decimal, help="固定价格止盈触发价")
    watch_parser.add_argument("--take-profit-ratio",
                              type=_decimal, help="固定价格止盈触发后的卖出比例")
    watch_parser.add_argument(
        "--trailing-drawdown", type=_decimal, help="峰值回撤止盈比例，例如 0.1 表示从峰值回撤 10%%")
    watch_parser.add_argument(
        "--trailing-drawdown-ratio", type=_decimal, help="峰值回撤止盈触发后的卖出比例")
    watch_parser.add_argument(
        "--trailing-activation-price", type=_decimal, help="可选，峰值回撤止盈开始生效前必须先达到的价格")
    watch_parser.add_argument(
        "--poll-interval", type=float, default=5.0, help="轮询间隔，单位秒")
    watch_parser.add_argument("--slippage-bps", type=_decimal,
                              default=Decimal("50"), help="允许的最差成交价滑点，单位为 bps")
    watch_parser.add_argument(
        "--dry-run", action="store_true", help="只评估规则，不真正提交卖单")
    watch_parser.add_argument(
        "--run-once", action="store_true", help="只执行一轮监控后立即退出")
    watch_parser.set_defaults(handler=handle_watch)

    return parser


def build_rules(args: argparse.Namespace) -> tuple[ExitRule, ...]:
    """把命令行参数转换成规则对象集合。"""
    rules: list[ExitRule] = []
    if args.breakeven_stop_ratio is not None:
        rules.append(ExitRule(kind=RuleKind.BREAKEVEN_STOP,
                     sell_ratio=args.breakeven_stop_ratio))
    if args.price_stop is not None or args.price_stop_ratio is not None:
        if args.price_stop is None or args.price_stop_ratio is None:
            raise ValueError(
                "--price-stop and --price-stop-ratio must be provided together")
        rules.append(
            ExitRule(
                kind=RuleKind.PRICE_STOP,
                sell_ratio=args.price_stop_ratio,
                trigger_price=args.price_stop,
            )
        )
    if args.take_profit is not None or args.take_profit_ratio is not None:
        if args.take_profit is None or args.take_profit_ratio is None:
            raise ValueError(
                "--take-profit and --take-profit-ratio must be provided together")
        rules.append(
            ExitRule(
                kind=RuleKind.TAKE_PROFIT,
                sell_ratio=args.take_profit_ratio,
                trigger_price=args.take_profit,
            )
        )
    if args.trailing_drawdown is not None or args.trailing_drawdown_ratio is not None or args.trailing_activation_price is not None:
        if args.trailing_drawdown is None or args.trailing_drawdown_ratio is None:
            raise ValueError(
                "--trailing-drawdown and --trailing-drawdown-ratio must be provided together"
            )
        rules.append(
            ExitRule(
                kind=RuleKind.TRAILING_TAKE_PROFIT,
                sell_ratio=args.trailing_drawdown_ratio,
                trigger_price=args.trailing_activation_price,
                drawdown_ratio=args.trailing_drawdown,
            )
        )
    if not rules:
        raise ValueError("at least one exit rule must be configured")
    return tuple(rules)


def build_position_provider(args: argparse.Namespace, gateway: PolymarketGateway):
    """根据是否提供手动参数，选择纯手动或官方持仓接口作为仓位来源。"""
    average_cost = args.average_cost
    size_override = getattr(args, "position_size", None)
    if size_override is not None and average_cost is not None:
        return ManualPositionProvider(size=size_override, average_cost=average_cost)
    return GatewayPositionProvider(
        gateway=gateway,
        average_cost_override=average_cost,
        size_override=size_override,
    )


def handle_positions(args: argparse.Namespace) -> int:
    """输出当前账号的持仓列表，便于人工核对均价和仓位。"""
    gateway = PolymarketGateway(PolymarketCredentials.from_env())
    positions = gateway.list_positions(size_threshold=args.size_threshold)
    if args.token_id:
        positions = [
            position for position in positions if position.token_id == args.token_id]
    payload = [
        {
            "token_id": position.token_id,
            "size": str(position.size),
            "average_cost": str(position.average_cost),
            "current_price": str(position.current_price),
            "current_value": str(position.current_value),
            "cash_pnl": str(position.cash_pnl),
            "percent_pnl": str(position.percent_pnl),
            "outcome": position.outcome,
            "market": position.market,
            "title": position.title,
            "slug": position.slug,
        }
        for position in positions
    ]
    print(json.dumps(payload, indent=2))
    return 0


def _serialize_levels(levels: tuple[OrderBookLevel, ...]) -> list[dict[str, str]]:
    """把盘口档位转换成便于 JSON 输出的结构。"""
    return [
        {"price": str(level.price), "size": str(level.size)}
        for level in levels
    ]


def _emit_watch_events(events) -> None:
    """统一输出监控事件，方便联调时直接对照网页盘口。"""
    for event in events:
        print(
            json.dumps(
                {
                    "token_id": event.token_id,
                    "rule": event.rule_name,
                    "status": event.status,
                    "best_bid": str(event.best_bid),
                    "best_ask": str(event.best_ask),
                    "top_bids": _serialize_levels(event.top_bids),
                    "top_asks": _serialize_levels(event.top_asks),
                    "trigger_price": str(event.trigger_price),
                    "requested_size": str(event.requested_size),
                    "filled_size": str(event.filled_size),
                    "message": event.message,
                }
            )
        )


def handle_watch(args: argparse.Namespace) -> int:
    """执行一次或持续执行 watch 任务。"""
    gateway = PolymarketGateway(PolymarketCredentials.from_env())
    rules = build_rules(args)
    provider = build_position_provider(args, gateway)
    executor = ExitExecutor(gateway=gateway, slippage_bps=args.slippage_bps)
    watcher = Watcher(quote_reader=gateway,
                      position_provider=provider, executor=executor)
    task = WatchTask(
        token_id=args.token_id,
        rules=rules,
        poll_interval_seconds=args.poll_interval,
        dry_run=args.dry_run,
    )
    if args.run_once:
        _emit_watch_events(watcher.run_cycle(task))
        return 0

    while True:
        _emit_watch_events(watcher.run_cycle(task))
        time.sleep(task.poll_interval_seconds)


def main(argv: Sequence[str] | None = None) -> int:
    """CLI 主入口：加载环境变量、解析参数并分发子命令。"""
    load_env_file()
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        return args.handler(args)
    except ValueError as exc:
        parser.error(str(exc))
        return 2
    except RuntimeError as exc:
        print(json.dumps({"error": str(exc)}), file=sys.stderr)
        return 1


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

from __future__ import annotations

"""卖单执行层，负责把规则决策转换成实际下单请求。"""

from dataclasses import dataclass
from decimal import Decimal, ROUND_DOWN
from typing import Protocol

from poly_shield.rules import ONE, ZERO


@dataclass(frozen=True)
class SellExecutionRequest:
    """一次卖出动作所需的最小输入集合。"""
    token_id: str
    size: Decimal
    best_bid: Decimal
    price_floor: Decimal
    rule_name: str
    dry_run: bool = False


@dataclass(frozen=True)
class ExecutionResult:
    """执行结果的统一返回结构，兼容 dry-run 和真实下单。"""
    status: str
    requested_size: Decimal
    filled_size: Decimal
    price_floor: Decimal
    order_id: str | None = None
    details: str = ""


class SellGateway(Protocol):
    """执行器依赖的最小交易网关接口。"""
    def get_tick_size(self, token_id: str) -> Decimal: ...

    def submit_market_sell(
        self, request: SellExecutionRequest) -> ExecutionResult: ...


def price_floor_from_bid(best_bid: Decimal, slippage_bps: Decimal) -> Decimal:
    """根据买一价和滑点容忍度，推导允许的最差成交价。"""
    if slippage_bps < ZERO:
        raise ValueError("slippage_bps cannot be negative")
    multiplier = ONE - (slippage_bps / Decimal("10000"))
    floor = best_bid * multiplier
    return floor if floor > ZERO else ZERO


def align_price_to_tick(price: Decimal, tick_size: Decimal) -> Decimal:
    """把价格向下对齐到交易所要求的最小跳价。"""
    if tick_size <= ZERO:
        raise ValueError("tick_size must be greater than zero")
    if price <= ZERO:
        return ZERO
    return (price / tick_size).to_integral_value(rounding=ROUND_DOWN) * tick_size


@dataclass(frozen=True)
class ExitExecutor:
    """负责生成卖单请求，并在 dry-run/真实下单之间切换。"""
    gateway: SellGateway
    slippage_bps: Decimal

    def build_request(self, *, token_id: str, size: Decimal, best_bid: Decimal, rule_name: str, dry_run: bool) -> SellExecutionRequest:
        """根据当前盘口、滑点和跳价规则构造卖单请求。"""
        tick_size = self.gateway.get_tick_size(token_id)
        raw_floor = price_floor_from_bid(best_bid, self.slippage_bps)
        price_floor = align_price_to_tick(raw_floor, tick_size)
        return SellExecutionRequest(
            token_id=token_id,
            size=size,
            best_bid=best_bid,
            price_floor=price_floor,
            rule_name=rule_name,
            dry_run=dry_run,
        )

    def execute(self, request: SellExecutionRequest) -> ExecutionResult:
        """执行请求；dry-run 时返回模拟结果，实盘时调用交易网关。"""
        if request.dry_run:
            return ExecutionResult(
                status="dry-run",
                requested_size=request.size,
                filled_size=ZERO,
                price_floor=request.price_floor,
                details=f"dry-run for {request.rule_name}",
            )
        return self.gateway.submit_market_sell(request)

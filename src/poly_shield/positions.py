from __future__ import annotations

"""持仓模型与持仓来源抽象。"""

from dataclasses import dataclass, replace
from decimal import Decimal
from typing import Protocol

from poly_shield.rules import ZERO


@dataclass(frozen=True)
class PositionRecord:
    """统一后的持仓数据结构，兼容手动输入和官方接口返回。"""
    token_id: str
    size: Decimal
    average_cost: Decimal = ZERO
    current_price: Decimal = ZERO
    current_value: Decimal = ZERO
    cash_pnl: Decimal = ZERO
    percent_pnl: Decimal = ZERO
    outcome: str | None = None
    market: str | None = None
    title: str | None = None
    event_slug: str | None = None
    slug: str | None = None
    proxy_wallet: str | None = None


class PositionReader(Protocol):
    """能够读取单个或多个持仓的接口约束。"""
    def get_position(self, token_id: str) -> PositionRecord: ...

    def list_positions(
        self, *, size_threshold: Decimal = ZERO) -> list[PositionRecord]: ...


class PositionProvider(Protocol):
    """watch 流程依赖的最小持仓提供接口。"""
    def get_position(self, token_id: str) -> PositionRecord: ...


@dataclass(frozen=True)
class ManualPositionProvider:
    """完全使用用户手动输入的仓位和均价。"""
    size: Decimal
    average_cost: Decimal = ZERO

    def get_position(self, token_id: str) -> PositionRecord:
        return PositionRecord(token_id=token_id, size=self.size, average_cost=self.average_cost)


@dataclass(frozen=True)
class GatewayPositionProvider:
    """优先使用官方持仓接口，并允许局部覆盖仓位或均价。"""
    gateway: PositionReader
    average_cost_override: Decimal | None = None
    size_override: Decimal | None = None

    def get_position(self, token_id: str) -> PositionRecord:
        """当只覆盖一个字段时，缺失部分仍然从官方持仓接口补齐。"""
        needs_gateway = self.size_override is None or self.average_cost_override is None
        if needs_gateway:
            position = self.gateway.get_position(token_id)
        else:
            position = PositionRecord(
                token_id=token_id, size=self.size_override or ZERO, average_cost=self.average_cost_override or ZERO)
        return replace(
            position,
            size=self.size_override if self.size_override is not None else position.size,
            average_cost=self.average_cost_override if self.average_cost_override is not None else position.average_cost,
        )

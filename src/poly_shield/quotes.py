from __future__ import annotations

"""盘口快照模型，供 watch 流程在不同网关之间复用。"""

from dataclasses import dataclass, field
from decimal import Decimal

from poly_shield.rules import ZERO


@dataclass(frozen=True)
class OrderBookLevel:
    """单档盘口。"""
    price: Decimal
    size: Decimal


@dataclass(frozen=True)
class QuoteSnapshot:
    """监控流程使用的盘口摘要，包含最优价和顶部若干档。"""
    market_id: str | None = None
    best_bid: Decimal = ZERO
    best_ask: Decimal = ZERO
    top_bids: tuple[OrderBookLevel, ...] = field(default_factory=tuple)
    top_asks: tuple[OrderBookLevel, ...] = field(default_factory=tuple)

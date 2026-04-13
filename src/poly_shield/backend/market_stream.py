from __future__ import annotations

"""Polymarket 市场 websocket 适配层。"""

import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal
from time import monotonic
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import connect

from poly_shield.quotes import OrderBookLevel, QuoteSnapshot
from poly_shield.rules import ZERO


MARKET_WS_ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/market"


def _decimal(value: Any, *, default: Decimal = ZERO) -> Decimal:
    if value in {None, ""}:
        return default
    return Decimal(str(value))


def _sorted_levels(levels: list[dict[str, Any]], *, reverse: bool, depth: int) -> tuple[OrderBookLevel, ...]:
    ordered = sorted(levels, key=lambda level: _decimal(
        level.get("price")), reverse=reverse)
    return tuple(
        OrderBookLevel(price=_decimal(level.get("price")),
                       size=_decimal(level.get("size")))
        for level in ordered[:depth]
    )


@dataclass
class PolymarketMarketStream:
    """订阅 Polymarket 市场频道，并将消息归一化为 QuoteSnapshot。"""

    token_ids: tuple[str, ...]
    endpoint: str = MARKET_WS_ENDPOINT
    depth: int = 3
    custom_feature_enabled: bool = True
    ping_interval_seconds: float = 10.0
    snapshots: dict[str, QuoteSnapshot] = field(default_factory=dict)

    def subscription_payload(self) -> dict[str, Any]:
        return {
            "assets_ids": list(self.token_ids),
            "type": "market",
            "custom_feature_enabled": self.custom_feature_enabled,
        }

    async def pump_quotes(
        self,
        *,
        stop_event: asyncio.Event,
        on_quote: Callable[[str, QuoteSnapshot], Awaitable[None]],
    ) -> None:
        """持续消费 websocket 消息，直到 stop_event 被置位。"""
        async with connect(self.endpoint) as websocket:
            await websocket.send(json.dumps(self.subscription_payload()))
            last_ping_at = monotonic()
            while not stop_event.is_set():
                try:
                    raw_message = await asyncio.wait_for(websocket.recv(), timeout=1.0)
                except TimeoutError:
                    if monotonic() - last_ping_at >= self.ping_interval_seconds:
                        await websocket.send("PING")
                        last_ping_at = monotonic()
                    continue
                for token_id, quote in self.extract_quotes(raw_message):
                    await on_quote(token_id, quote)

    def extract_quotes(self, raw_message: str | bytes) -> list[tuple[str, QuoteSnapshot]]:
        """解析一帧 websocket 消息并产出盘口更新。"""
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8")
        if raw_message == "PONG":
            return []
        payload = json.loads(raw_message)
        messages = payload if isinstance(payload, list) else [payload]
        updates: list[tuple[str, QuoteSnapshot]] = []
        for message in messages:
            updates.extend(self._handle_message(message))
        return updates

    def _handle_message(self, message: dict[str, Any]) -> list[tuple[str, QuoteSnapshot]]:
        event_type = message.get("event_type")
        if event_type == "book":
            token_id = str(message["asset_id"])
            top_bids = _sorted_levels(message.get(
                "bids", []), reverse=True, depth=self.depth)
            top_asks = _sorted_levels(message.get(
                "asks", []), reverse=False, depth=self.depth)
            quote = QuoteSnapshot(
                market_id=str(message.get("market") or "") or None,
                best_bid=top_bids[0].price if top_bids else ZERO,
                best_ask=top_asks[0].price if top_asks else ZERO,
                top_bids=top_bids,
                top_asks=top_asks,
            )
            self.snapshots[token_id] = quote
            return [(token_id, quote)]
        if event_type == "best_bid_ask":
            token_id = str(message["asset_id"])
            return [(token_id, self._update_best_bid_ask(token_id, message))]
        if event_type == "price_change":
            updates: list[tuple[str, QuoteSnapshot]] = []
            for change in message.get("price_changes", []):
                token_id = str(change["asset_id"])
                updates.append(
                    (token_id, self._update_best_bid_ask(token_id, change)))
            return updates
        return []

    def _update_best_bid_ask(self, token_id: str, message: dict[str, Any]) -> QuoteSnapshot:
        existing = self.snapshots.get(token_id, QuoteSnapshot())
        quote = QuoteSnapshot(
            market_id=str(message.get("market")
                          or existing.market_id or "") or None,
            best_bid=_decimal(message.get("best_bid"),
                              default=existing.best_bid),
            best_ask=_decimal(message.get("best_ask"),
                              default=existing.best_ask),
            top_bids=existing.top_bids,
            top_asks=existing.top_asks,
        )
        self.snapshots[token_id] = quote
        return quote

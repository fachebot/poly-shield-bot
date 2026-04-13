from __future__ import annotations

"""Polymarket 用户 websocket 适配层。"""

import asyncio
import json
from dataclasses import dataclass, field
from decimal import Decimal
from time import monotonic
from typing import Any, Awaitable, Callable

from websockets.asyncio.client import connect

from poly_shield.rules import ZERO


USER_WS_ENDPOINT = "wss://ws-subscriptions-clob.polymarket.com/ws/user"


def _decimal(value: Any, *, default: Decimal = ZERO) -> Decimal:
    if value in {None, ""}:
        return default
    return Decimal(str(value))


@dataclass(frozen=True)
class UserStreamAuth:
    """用户频道订阅所需的 API 凭证。"""

    api_key: str
    api_secret: str
    api_passphrase: str


@dataclass(frozen=True)
class UserStreamEvent:
    """归一化后的 user channel 事件。"""

    event_type: str
    status: str
    order_id: str | None = None
    related_order_ids: tuple[str, ...] = field(default_factory=tuple)
    token_id: str = ""
    market_id: str | None = None
    requested_size: Decimal = ZERO
    filled_size: Decimal = ZERO
    event_price: Decimal = ZERO
    message: str = ""

    @property
    def is_terminal(self) -> bool:
        if self.event_type == "trade":
            return self.status in {"confirmed", "failed"}
        if self.event_type == "order":
            return self.status == "cancellation"
        return False


@dataclass
class PolymarketUserStream:
    """订阅 Polymarket 用户频道，并将消息归一化为订单/成交事件。"""

    market_ids: tuple[str, ...]
    auth: UserStreamAuth
    endpoint: str = USER_WS_ENDPOINT
    ping_interval_seconds: float = 10.0

    def subscription_payload(self) -> dict[str, Any]:
        return {
            "auth": {
                "apiKey": self.auth.api_key,
                "secret": self.auth.api_secret,
                "passphrase": self.auth.api_passphrase,
            },
            "markets": list(self.market_ids),
            "type": "user",
        }

    async def pump_events(
        self,
        *,
        stop_event: asyncio.Event,
        on_event: Callable[[UserStreamEvent], Awaitable[None]],
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
                for event in self.extract_events(raw_message):
                    await on_event(event)

    def extract_events(self, raw_message: str | bytes) -> list[UserStreamEvent]:
        """解析一帧 websocket 消息并产出用户事件。"""
        if isinstance(raw_message, bytes):
            raw_message = raw_message.decode("utf-8")
        if raw_message == "PONG":
            return []
        payload = json.loads(raw_message)
        messages = payload if isinstance(payload, list) else [payload]
        updates: list[UserStreamEvent] = []
        for message in messages:
            parsed = self._parse_message(message)
            if parsed is not None:
                updates.append(parsed)
        return updates

    def _parse_message(self, message: dict[str, Any]) -> UserStreamEvent | None:
        event_type = str(message.get("event_type") or "").lower()
        if event_type == "trade":
            return self._parse_trade(message)
        if event_type == "order":
            return self._parse_order(message)
        return None

    def _parse_trade(self, message: dict[str, Any]) -> UserStreamEvent:
        maker_orders = message.get("maker_orders") or []
        related_order_ids = tuple(
            order_id
            for order_id in [
                str(message.get("taker_order_id") or "") or None,
                *[
                    str(order.get("order_id") or "") or None
                    for order in maker_orders
                ],
            ]
            if order_id is not None
        )
        order_id = related_order_ids[0] if related_order_ids else None
        status = str(message.get("status") or "trade").lower()
        return UserStreamEvent(
            event_type="trade",
            status=status,
            order_id=order_id,
            related_order_ids=related_order_ids,
            token_id=str(message.get("asset_id") or ""),
            market_id=str(message.get("market") or "") or None,
            requested_size=_decimal(message.get("size")),
            filled_size=_decimal(message.get("size")),
            event_price=_decimal(message.get("price")),
            message=f"trade {status}",
        )

    def _parse_order(self, message: dict[str, Any]) -> UserStreamEvent:
        order_id = str(message.get("id") or "") or None
        status = str(message.get("type") or "order").lower()
        requested_size = _decimal(message.get(
            "original_size"), default=_decimal(message.get("size")))
        filled_size = _decimal(message.get("size_matched"))
        return UserStreamEvent(
            event_type="order",
            status=status,
            order_id=order_id,
            related_order_ids=(order_id,) if order_id is not None else (),
            token_id=str(message.get("asset_id") or ""),
            market_id=str(message.get("market") or "") or None,
            requested_size=requested_size,
            filled_size=filled_size,
            event_price=_decimal(message.get("price")),
            message=f"order {status}",
        )

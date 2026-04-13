from decimal import Decimal

from poly_shield.backend.user_stream import PolymarketUserStream, UserStreamAuth


def test_user_stream_builds_subscription_payload() -> None:
    stream = PolymarketUserStream(
        market_ids=("0xmarket-1", "0xmarket-2"),
        auth=UserStreamAuth(
            api_key="key",
            api_secret="secret",
            api_passphrase="passphrase",
        ),
    )

    assert stream.subscription_payload() == {
        "auth": {
            "apiKey": "key",
            "secret": "secret",
            "passphrase": "passphrase",
        },
        "markets": ["0xmarket-1", "0xmarket-2"],
        "type": "user",
    }


def test_user_stream_extracts_trade_and_order_updates() -> None:
    stream = PolymarketUserStream(
        market_ids=("0xmarket-1",),
        auth=UserStreamAuth(
            api_key="key",
            api_secret="secret",
            api_passphrase="passphrase",
        ),
    )

    events = stream.extract_events(
        """
        [
          {
            "event_type": "trade",
            "asset_id": "token-1",
            "market": "0xmarket-1",
            "status": "CONFIRMED",
            "size": "10",
            "price": "0.57",
            "taker_order_id": "order-1",
            "maker_orders": [{"order_id": "maker-1"}]
          },
          {
            "event_type": "order",
            "asset_id": "token-1",
            "market": "0xmarket-1",
            "id": "order-1",
            "type": "UPDATE",
            "original_size": "10",
            "size_matched": "5",
            "price": "0.57"
          }
        ]
        """
    )

    assert len(events) == 2
    assert events[0].event_type == "trade"
    assert events[0].status == "confirmed"
    assert events[0].related_order_ids == ("order-1", "maker-1")
    assert events[0].requested_size == Decimal("10")
    assert events[0].filled_size == Decimal("10")
    assert events[0].event_price == Decimal("0.57")
    assert events[0].is_terminal is True
    assert events[1].event_type == "order"
    assert events[1].status == "update"
    assert events[1].order_id == "order-1"
    assert events[1].filled_size == Decimal("5")
    assert events[1].is_terminal is False

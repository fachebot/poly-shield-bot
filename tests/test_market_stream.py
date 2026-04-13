import asyncio

from poly_shield.backend.market_stream import PolymarketMarketStream


def test_market_stream_builds_subscription_payload() -> None:
    stream = PolymarketMarketStream(token_ids=("token-1", "token-2"))

    assert stream.subscription_payload() == {
        "assets_ids": ["token-1", "token-2"],
        "type": "market",
        "custom_feature_enabled": True,
    }


def test_market_stream_extracts_book_and_best_bid_ask_updates() -> None:
    stream = PolymarketMarketStream(token_ids=("token-1",))

    book_updates = stream.extract_quotes(
        """
        {
          "event_type": "book",
          "asset_id": "token-1",
                    "market": "0xmarket-1",
          "bids": [{"price": "0.70", "size": "100"}, {"price": "0.69", "size": "20"}],
          "asks": [{"price": "0.72", "size": "50"}, {"price": "0.73", "size": "10"}]
        }
        """
    )
    best_bid_ask_updates = stream.extract_quotes(
        """
        {
          "event_type": "best_bid_ask",
          "asset_id": "token-1",
                    "market": "0xmarket-1",
          "best_bid": "0.71",
          "best_ask": "0.74"
        }
        """
    )

    assert book_updates[0][0] == "token-1"
    assert book_updates[0][1].market_id == "0xmarket-1"
    assert str(book_updates[0][1].best_bid) == "0.70"
    assert str(book_updates[0][1].best_ask) == "0.72"
    assert best_bid_ask_updates[0][1].market_id == "0xmarket-1"
    assert str(best_bid_ask_updates[0][1].best_bid) == "0.71"
    assert str(best_bid_ask_updates[0][1].best_ask) == "0.74"
    assert str(best_bid_ask_updates[0][1].top_bids[0].price) == "0.70"

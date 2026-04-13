import pytest

from decimal import Decimal

from poly_shield.config import PolymarketCredentials
from poly_shield.polymarket import PolymarketGateway, PolymarketRequestError
from poly_shield.positions import GatewayPositionProvider


def make_credentials() -> PolymarketCredentials:
    return PolymarketCredentials(
        host="https://clob.polymarket.com",
        data_api_url="https://data-api.polymarket.com",
        chain_id=137,
        private_key=None,
        api_key=None,
        api_secret=None,
        api_passphrase=None,
        funder="0x123",
        user_address="0x123",
        signature_type=None,
    )


def test_gateway_parses_positions_from_official_data_api(monkeypatch) -> None:
    gateway = PolymarketGateway(make_credentials())

    monkeypatch.setattr(
        gateway,
        "_data_api_get",
        lambda path, params: [
            {
                "proxyWallet": "0x123",
                "asset": "token-1",
                "conditionId": "0xmarket",
                "size": 12.5,
                "avgPrice": 0.44,
                "currentValue": 6.0,
                "cashPnl": 0.5,
                "percentPnl": 8.5,
                "curPrice": 0.48,
                "title": "Example Market",
                "slug": "example-market",
                "outcome": "Yes",
            }
        ],
    )

    positions = gateway.list_positions(size_threshold=Decimal("0"))

    assert len(positions) == 1
    assert positions[0].token_id == "token-1"
    assert positions[0].average_cost == Decimal("0.44")
    assert positions[0].current_price == Decimal("0.48")
    assert positions[0].market == "0xmarket"


def test_gateway_position_provider_uses_official_average_cost(monkeypatch) -> None:
    gateway = PolymarketGateway(make_credentials())
    provider = GatewayPositionProvider(gateway=gateway)

    monkeypatch.setattr(
        gateway,
        "get_position",
        lambda token_id: gateway._parse_position(
            {
                "asset": token_id,
                "size": 20,
                "avgPrice": 0.51,
                "curPrice": 0.55,
                "outcome": "No",
            }
        ),
    )

    position = provider.get_position("token-2")

    assert position.size == Decimal("20")
    assert position.average_cost == Decimal("0.51")


def test_gateway_position_provider_allows_manual_size_override_with_auto_average_cost(monkeypatch) -> None:
    gateway = PolymarketGateway(make_credentials())
    provider = GatewayPositionProvider(
        gateway=gateway, size_override=Decimal("7"))

    monkeypatch.setattr(
        gateway,
        "get_position",
        lambda token_id: gateway._parse_position(
            {
                "asset": token_id,
                "size": 20,
                "avgPrice": 0.33,
            }
        ),
    )

    position = provider.get_position("token-3")

    assert position.size == Decimal("7")
    assert position.average_cost == Decimal("0.33")


def test_gateway_surfaces_geoblock_positions_error(monkeypatch) -> None:
    gateway = PolymarketGateway(make_credentials())

    class FakePolyApiException(Exception):
        def __init__(self, status_code: int | None, error_msg):
            self.status_code = status_code
            self.error_msg = error_msg

    class FakeHttpHelpers:
        @staticmethod
        def get(url: str):
            raise FakePolyApiException(403, "error code: 1010")

    class FakeBundle:
        http_helpers = FakeHttpHelpers
        PolyApiException = FakePolyApiException

    monkeypatch.setitem(gateway.__dict__, "_bundle", FakeBundle)

    with pytest.raises(PolymarketRequestError, match="Cloudflare/geoblock error 1010"):
        gateway._data_api_get("/positions", {"user": "0x123"})


def test_gateway_selects_highest_bid_from_order_book(monkeypatch) -> None:
    gateway = PolymarketGateway(make_credentials())

    class FakeClient:
        @staticmethod
        def get_order_book(token_id: str):
            return {
                "bids": [
                    {"price": "0.001", "size": "10"},
                    {"price": "0.062", "size": "20"},
                    {"price": "0.054", "size": "30"},
                ]
            }

    monkeypatch.setitem(gateway.__dict__, "_readonly_client", FakeClient)

    assert gateway.get_best_bid("token-1") == Decimal("0.062")

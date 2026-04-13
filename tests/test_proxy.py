import os

from poly_shield.config import PolymarketCredentials
from poly_shield.polymarket import PolymarketGateway


def make_credentials(*, http_proxy: str | None = None, https_proxy: str | None = None, no_proxy: str | None = None) -> PolymarketCredentials:
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
        http_proxy=http_proxy,
        https_proxy=https_proxy,
        no_proxy=no_proxy,
    )


def test_credentials_apply_proxy_environment(monkeypatch) -> None:
    monkeypatch.delenv("HTTP_PROXY", raising=False)
    monkeypatch.delenv("http_proxy", raising=False)
    monkeypatch.delenv("HTTPS_PROXY", raising=False)
    monkeypatch.delenv("https_proxy", raising=False)
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)

    credentials = make_credentials(
        http_proxy="http://127.0.0.1:7890",
        https_proxy="http://127.0.0.1:7890",
        no_proxy="localhost,127.0.0.1",
    )

    credentials.apply_proxy_environment()

    assert os.environ["HTTP_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["http_proxy"] == "http://127.0.0.1:7890"
    assert os.environ["HTTPS_PROXY"] == "http://127.0.0.1:7890"
    assert os.environ["https_proxy"] == "http://127.0.0.1:7890"
    assert os.environ["NO_PROXY"] == "localhost,127.0.0.1"
    assert os.environ["no_proxy"] == "localhost,127.0.0.1"


def test_gateway_rebuilds_sdk_http_client_when_proxy_is_configured() -> None:
    closed = []

    class ExistingClient:
        def close(self) -> None:
            closed.append(True)

    created = []

    class ReplacementClient:
        def __init__(self, **kwargs):
            self.kwargs = kwargs
            created.append(kwargs)

    class FakeHttpx:
        Client = ReplacementClient

    class FakeHttpHelpers:
        _http_client = ExistingClient()

    class FakeBundle:
        http_helpers = FakeHttpHelpers
        httpx = FakeHttpx

    gateway = PolymarketGateway(make_credentials(
        https_proxy="http://127.0.0.1:7890"))

    gateway._configure_sdk_http_proxy(FakeBundle)

    assert closed == [True]
    assert created == [{"http2": True, "trust_env": True}]
    assert isinstance(FakeHttpHelpers._http_client, ReplacementClient)


def test_gateway_skips_sdk_http_client_rebuild_without_proxy_config() -> None:
    class ExistingClient:
        def close(self) -> None:  # pragma: no cover - should not be called
            raise AssertionError(
                "close should not be called when no proxy is configured")

    class FakeHttpHelpers:
        _http_client = ExistingClient()

    class FakeBundle:
        http_helpers = FakeHttpHelpers
        httpx = object()

    gateway = PolymarketGateway(make_credentials())

    gateway._configure_sdk_http_proxy(FakeBundle)

    assert isinstance(FakeHttpHelpers._http_client, ExistingClient)

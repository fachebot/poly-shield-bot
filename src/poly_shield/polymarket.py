from __future__ import annotations

"""Polymarket 网关实现，负责对接官方 CLOB SDK 和持仓接口。"""

from dataclasses import dataclass
from decimal import Decimal
from functools import cached_property
from typing import Any
from urllib.parse import urlencode

from poly_shield.config import PolymarketCredentials
from poly_shield.executor import ExecutionResult, SellExecutionRequest
from poly_shield.positions import PositionRecord
from poly_shield.quotes import OrderBookLevel, QuoteSnapshot
from poly_shield.rules import ZERO


class PolymarketConfigurationError(RuntimeError):
    """本地配置不足，导致网关无法初始化。"""


class PolymarketDependencyError(RuntimeError):
    """缺少 py-clob-client 依赖。"""


class PolymarketRequestError(RuntimeError):
    """访问 Polymarket 接口时发生错误。"""


@dataclass(frozen=True)
class _SdkBundle:
    """把官方 SDK 里会用到的对象集中打包，便于懒加载和测试替身。"""
    ClobClient: Any
    ApiCreds: Any
    BalanceAllowanceParams: Any
    AssetType: Any
    MarketOrderArgs: Any
    OrderType: Any
    PolyApiException: Any
    SELL: Any
    http_helpers: Any
    httpx: Any


def _sdk() -> _SdkBundle:
    """延迟导入官方 SDK，避免在纯单测场景里强依赖真实环境。"""
    try:
        from py_clob_client.client import ClobClient
        from py_clob_client.clob_types import ApiCreds, BalanceAllowanceParams, MarketOrderArgs, OrderType
        from py_clob_client.clob_types import AssetType
        from py_clob_client.exceptions import PolyApiException
        from py_clob_client.http_helpers import helpers as http_helpers
        from py_clob_client.order_builder.constants import SELL
        import httpx
    except ImportError as exc:  # pragma: no cover - exercised only in real runtime
        raise PolymarketDependencyError(
            "py-clob-client is required for live Polymarket access. Install project dependencies first."
        ) from exc
    return _SdkBundle(
        ClobClient=ClobClient,
        ApiCreds=ApiCreds,
        BalanceAllowanceParams=BalanceAllowanceParams,
        AssetType=AssetType,
        MarketOrderArgs=MarketOrderArgs,
        OrderType=OrderType,
        PolyApiException=PolyApiException,
        SELL=SELL,
        http_helpers=http_helpers,
        httpx=httpx,
    )


def _decimal_from_value(value: Any, *, default: Decimal = ZERO) -> Decimal:
    """把 SDK 或接口返回值安全转换成 Decimal。"""
    if value in {None, ""}:
        return default
    return Decimal(str(value))


def _extract_field(payload: Any, *names: str) -> Any:
    """兼容 dict / SDK 对象两种返回结构，按候选字段名取值。"""
    if isinstance(payload, dict):
        for name in names:
            if name in payload:
                return payload[name]
        return None
    for name in names:
        if hasattr(payload, name):
            return getattr(payload, name)
    return None


def _extract_first_level(book: Any, side: str) -> Any:
    """提取某一侧最优档位。"""
    levels = _extract_field(book, side)
    if not levels:
        return None
    side_multiplier = Decimal("1") if side == "bids" else Decimal("-1")
    return max(
        levels,
        key=lambda level: _decimal_from_value(_extract_field(level, "price")) * side_multiplier,
    )


def _extract_sorted_levels(book: Any, side: str) -> list[Any]:
    """把盘口按价格排序，bids 从高到低，asks 从低到高。"""
    levels = _extract_field(book, side)
    if not levels:
        return []
    return sorted(
        levels,
        key=lambda level: _decimal_from_value(_extract_field(level, "price")),
        reverse=side == "bids",
    )


def _parse_order_book_level(level: Any) -> OrderBookLevel:
    """把 SDK 的盘口档位转换成项目内部统一结构。"""
    return OrderBookLevel(
        price=_decimal_from_value(_extract_field(level, "price")),
        size=_decimal_from_value(_extract_field(level, "size")),
    )


@dataclass
class PolymarketGateway:
    """项目内的 Polymarket 统一网关实现。"""
    credentials: PolymarketCredentials
    default_tick_size: Decimal = Decimal("0.001")

    @cached_property
    def _bundle(self) -> _SdkBundle:
        """首次使用时初始化 SDK，并让其继承当前代理配置。"""
        self.credentials.apply_proxy_environment()
        bundle = _sdk()
        self._configure_sdk_http_proxy(bundle)
        return bundle

    @cached_property
    def _readonly_client(self) -> Any:
        """只读客户端用于盘口、tick size 等公开接口。"""
        return self._bundle.ClobClient(self.credentials.host)

    @cached_property
    def _trading_client(self) -> Any:
        """交易客户端用于余额、下单和 heartbeat，需要签名能力。"""
        if not self.credentials.private_key:
            raise PolymarketConfigurationError(
                "POLY_PRIVATE_KEY or PK is required for authenticated operations")
        if not self.credentials.funder:
            raise PolymarketConfigurationError(
                "POLY_FUNDER or FUNDER is required for authenticated operations")

        kwargs: dict[str, Any] = {
            "key": self.credentials.private_key,
            "chain_id": self.credentials.chain_id,
            "funder": self.credentials.funder,
        }
        if self.credentials.signature_type is not None:
            kwargs["signature_type"] = self.credentials.signature_type
        if self.credentials.has_api_creds:
            kwargs["creds"] = self._bundle.ApiCreds(
                api_key=self.credentials.api_key,
                api_secret=self.credentials.api_secret,
                api_passphrase=self.credentials.api_passphrase,
            )

        client = self._bundle.ClobClient(self.credentials.host, **kwargs)
        if not self.credentials.has_api_creds:
            client.set_api_creds(client.create_or_derive_api_creds())
        return client

    def get_quote_snapshot(self, token_id: str, *, depth: int = 3) -> QuoteSnapshot:
        """读取并整理盘口摘要，供 watch 输出与规则判定复用。"""
        book = self._get_order_book(token_id)
        bid_levels = tuple(
            _parse_order_book_level(level)
            for level in _extract_sorted_levels(book, "bids")[:depth]
        )
        ask_levels = tuple(
            _parse_order_book_level(level)
            for level in _extract_sorted_levels(book, "asks")[:depth]
        )
        return QuoteSnapshot(
            best_bid=bid_levels[0].price if bid_levels else ZERO,
            best_ask=ask_levels[0].price if ask_levels else ZERO,
            top_bids=bid_levels,
            top_asks=ask_levels,
        )

    def get_best_bid(self, token_id: str) -> Decimal:
        """保留一个简化接口，供只需要买一价的逻辑直接调用。"""
        best_level = _extract_first_level(self._get_order_book(token_id), "bids")
        if best_level is None:
            return ZERO
        return _decimal_from_value(_extract_field(best_level, "price"))

    def get_tick_size(self, token_id: str) -> Decimal:
        getter = getattr(self._readonly_client, "get_tick_size", None)
        if not callable(getter):
            return self.default_tick_size
        tick_size = getter(token_id)
        return _decimal_from_value(tick_size, default=self.default_tick_size)

    def list_positions(self, *, size_threshold: Decimal = ZERO) -> list[PositionRecord]:
        """分页读取官方持仓接口，并统一成项目内部模型。"""
        positions: list[PositionRecord] = []
        offset = 0
        page_size = 100
        while True:
            response = self._data_api_get(
                "/positions",
                {
                    "user": self._require_user_address(),
                    "sizeThreshold": str(size_threshold),
                    "limit": str(page_size),
                    "offset": str(offset),
                    "sortBy": "TOKENS",
                    "sortDirection": "DESC",
                },
            )
            if not isinstance(response, list):
                raise PolymarketRequestError(
                    "unexpected positions response from data API")
            page = [self._parse_position(item) for item in response]
            positions.extend(page)
            if len(page) < page_size:
                return positions
            offset += page_size

    def get_position(self, token_id: str) -> PositionRecord:
        for position in self.list_positions(size_threshold=ZERO):
            if position.token_id == token_id:
                return position
        raise PolymarketRequestError(
            f"no current position found for token {token_id}")

    def get_position_size(self, token_id: str) -> Decimal:
        return self.get_position(token_id).size

    def get_balance_allowance(self, token_id: str) -> Decimal:
        """读取指定条件 token 的可卖余额。"""
        params = self._bundle.BalanceAllowanceParams(
            asset_type=self._bundle.AssetType.CONDITIONAL,
            token_id=token_id,
        )
        try:
            response = self._trading_client.get_balance_allowance(
                params=params)
        except Exception as exc:  # pragma: no cover - depends on remote API
            raise PolymarketRequestError(
                f"failed to fetch conditional balance for token {token_id}: {exc}") from exc
        return _decimal_from_value(_extract_field(response, "balance"))

    def submit_market_sell(self, request: SellExecutionRequest) -> ExecutionResult:
        """按 FAK 市价单语义提交卖单。"""
        order_type = getattr(self._bundle.OrderType, "FAK")
        try:
            order_args = self._bundle.MarketOrderArgs(
                token_id=request.token_id,
                amount=float(request.size),
                price=float(request.price_floor),
                side=self._bundle.SELL,
                order_type=order_type,
            )
            signed_order = self._trading_client.create_market_order(order_args)
            response = self._trading_client.post_order(
                signed_order, orderType=order_type)
        except Exception as exc:  # pragma: no cover - depends on remote API
            raise PolymarketRequestError(
                f"failed to submit sell order for token {request.token_id}: {exc}") from exc
        status = _extract_field(response, "status") or "submitted"
        filled_size = self._extract_filled_size(response, request.size)
        return ExecutionResult(
            status=str(status),
            requested_size=request.size,
            filled_size=filled_size,
            price_floor=request.price_floor,
            order_id=_extract_field(response, "orderID", "id"),
            details=str(response),
        )

    def post_heartbeat(self, heartbeat_id: str | None = None) -> dict[str, Any]:
        try:
            return self._trading_client.post_heartbeat(heartbeat_id)
        except Exception as exc:  # pragma: no cover - depends on remote API
            raise PolymarketRequestError(
                f"failed to post heartbeat: {exc}") from exc

    def _get_order_book(self, token_id: str) -> Any:
        """统一封装 order book 拉取，便于集中处理错误。"""
        try:
            return self._readonly_client.get_order_book(token_id)
        except Exception as exc:  # pragma: no cover - depends on remote API
            raise PolymarketRequestError(
                f"failed to fetch order book for token {token_id}: {exc}") from exc

    def _data_api_get(self, path: str, params: dict[str, str]) -> Any:
        """访问官方 data-api，并把常见网络错误转换成项目内部异常。"""
        self.credentials.apply_proxy_environment()
        base_url = self.credentials.data_api_url.rstrip("/")
        query = urlencode(params, doseq=True)
        url = f"{base_url}{path}?{query}"
        try:
            return self._bundle.http_helpers.get(url)
        except self._bundle.PolyApiException as exc:  # pragma: no cover - depends on remote API
            details = exc.error_msg
            details_text = details if isinstance(details, str) else str(details)
            if exc.status_code == 403 and "1010" in details_text:
                raise PolymarketRequestError(
                    "Polymarket data API blocked this request with Cloudflare/geoblock error 1010. "
                    "Run the bot from an allowed network, configure POLY_HTTPS_PROXY, or pass both --position-size and --average-cost to avoid relying on /positions."
                ) from exc
            if exc.status_code is None:
                raise PolymarketRequestError(
                    f"failed to reach Polymarket data API at {url}: {details_text}"
                ) from exc
            raise PolymarketRequestError(
                f"data API request failed for {url}: {exc.status_code} {details_text}"
            ) from exc

    def _parse_position(self, payload: Any) -> PositionRecord:
        """把官方 positions 接口返回映射成项目内部持仓结构。"""
        return PositionRecord(
            token_id=str(_extract_field(payload, "asset") or ""),
            size=_decimal_from_value(_extract_field(payload, "size")),
            average_cost=_decimal_from_value(
                _extract_field(payload, "avgPrice")),
            current_price=_decimal_from_value(
                _extract_field(payload, "curPrice", "currPrice")),
            current_value=_decimal_from_value(
                _extract_field(payload, "currentValue")),
            cash_pnl=_decimal_from_value(_extract_field(payload, "cashPnl")),
            percent_pnl=_decimal_from_value(
                _extract_field(payload, "percentPnl")),
            outcome=_extract_field(payload, "outcome"),
            market=_extract_field(payload, "conditionId"),
            title=_extract_field(payload, "title"),
            slug=_extract_field(payload, "slug"),
            proxy_wallet=_extract_field(payload, "proxyWallet"),
        )

    def _require_user_address(self) -> str:
        """确保持仓查询至少能拿到一个可识别的钱包地址。"""
        user_address = self.credentials.user_address or self.credentials.funder
        if not user_address:
            raise PolymarketConfigurationError(
                "POLY_USER_ADDRESS, POLY_USER, or POLY_FUNDER is required for positions queries"
            )
        return user_address

    def _configure_sdk_http_proxy(self, bundle: _SdkBundle) -> None:
        """让官方 SDK 的全局 HTTP 客户端继承本项目的代理配置。"""
        if not self.credentials.has_proxy_config:
            return
        http_helpers = bundle.http_helpers
        existing_client = getattr(http_helpers, "_http_client", None)
        if existing_client is not None and hasattr(existing_client, "close"):
            existing_client.close()
        http_helpers._http_client = bundle.httpx.Client(
            http2=True, trust_env=True)

    def _extract_filled_size(self, response: Any, requested_size: Decimal) -> Decimal:
        """尽量从不同格式的下单响应里提取实际成交数量。"""
        for field_name in ("makingAmount", "takingAmount", "filled_size", "size_matched"):
            value = _extract_field(response, field_name)
            if value not in {None, "", "0", 0}:
                filled = _decimal_from_value(value)
                return requested_size if filled > requested_size else filled
        status = _extract_field(response, "status")
        if status == "matched":
            return requested_size
        return ZERO

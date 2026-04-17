from __future__ import annotations

"""运行时配置加载，负责从 .env 和环境变量中组装 Polymarket 凭证。"""

import os
from dataclasses import dataclass
from pathlib import Path

from eth_account import Account

from poly_shield.secret_store import LocalSecretStore


DEFAULT_ENV_FILE = Path(".env")


def load_env_file(path: Path = DEFAULT_ENV_FILE) -> None:
    """读取本地 .env，并把未显式设置过的键写入环境变量。"""
    if not path.exists():
        return
    for raw_line in path.read_text(encoding="utf-8").splitlines():
        line = raw_line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        cleaned = value.strip().strip('"').strip("'")
        os.environ.setdefault(key.strip(), cleaned)


def _env(*names: str, default: str | None = None) -> str | None:
    """按优先顺序读取多个环境变量别名。"""
    for name in names:
        value = os.getenv(name)
        if value not in {None, ""}:
            return value
    return default


def apply_proxy_environment_values(
    *,
    http_proxy: str | None = None,
    https_proxy: str | None = None,
    no_proxy: str | None = None,
) -> None:
    """把代理配置同步到标准 HTTP(S)_PROXY 环境变量。"""
    if http_proxy:
        os.environ["HTTP_PROXY"] = http_proxy
        os.environ["http_proxy"] = http_proxy
    if https_proxy:
        os.environ["HTTPS_PROXY"] = https_proxy
        os.environ["https_proxy"] = https_proxy
    if no_proxy:
        os.environ["NO_PROXY"] = no_proxy
        os.environ["no_proxy"] = no_proxy


def apply_proxy_environment_from_env() -> None:
    """把项目代理环境变量同步到标准 HTTP(S)_PROXY 环境变量。"""
    apply_proxy_environment_values(
        http_proxy=_env("POLY_HTTP_PROXY", "HTTP_PROXY", "http_proxy"),
        https_proxy=_env("POLY_HTTPS_PROXY", "HTTPS_PROXY", "https_proxy"),
        no_proxy=_env("POLY_NO_PROXY", "NO_PROXY", "no_proxy"),
    )


def _signer_address_from_private_key(private_key: str | None) -> str | None:
    if not private_key:
        return None
    try:
        return Account.from_key(private_key).address
    except Exception:
        return None


def _derive_user_address(
    *,
    signer_address: str | None,
    funder: str | None,
    signature_type: int | None,
) -> str | None:
    # signature_type 1/2 uses proxy wallet semantics; user should be funder/proxy wallet.
    if signature_type in {1, 2}:
        return funder
    # direct-wallet mode defaults to signer; if unavailable, fall back to funder.
    return signer_address or funder


@dataclass(frozen=True)
class PolymarketCredentials:
    """统一管理 CLOB、持仓接口和代理所需的运行时配置。"""
    host: str
    data_api_url: str
    chain_id: int
    private_key: str | None
    api_key: str | None
    api_secret: str | None
    api_passphrase: str | None
    funder: str | None
    user_address: str | None
    signature_type: int | None
    http_proxy: str | None = None
    https_proxy: str | None = None
    no_proxy: str | None = None

    @classmethod
    def from_env(cls) -> "PolymarketCredentials":
        """从环境变量构造一份完整的凭证对象。"""
        host = _env("POLY_CLOB_HOST", "CLOB_API_URL",
                    default="https://clob.polymarket.com")
        data_api_url = _env("POLY_DATA_API_URL",
                            default="https://data-api.polymarket.com")
        chain_id = int(_env("POLY_CHAIN_ID", default="137") or "137")
        signature_raw = _env("POLY_SIGNATURE_TYPE")
        signature_type = int(signature_raw) if signature_raw else None
        funder = _env("POLY_FUNDER", "FUNDER")
        private_key = LocalSecretStore.default().load_private_key()
        signer_address = _signer_address_from_private_key(private_key)
        return cls(
            host=host or "https://clob.polymarket.com",
            data_api_url=data_api_url or "https://data-api.polymarket.com",
            chain_id=chain_id,
            private_key=private_key,
            api_key=_env("POLY_API_KEY", "CLOB_API_KEY"),
            api_secret=_env("POLY_API_SECRET", "CLOB_SECRET"),
            api_passphrase=_env("POLY_API_PASSPHRASE", "CLOB_PASS_PHRASE"),
            funder=funder,
            user_address=_derive_user_address(
                signer_address=signer_address,
                funder=funder,
                signature_type=signature_type,
            ),
            signature_type=signature_type,
            http_proxy=_env("POLY_HTTP_PROXY", "HTTP_PROXY", "http_proxy"),
            https_proxy=_env("POLY_HTTPS_PROXY", "HTTPS_PROXY", "https_proxy"),
            no_proxy=_env("POLY_NO_PROXY", "NO_PROXY", "no_proxy"),
        )

    @property
    def has_api_creds(self) -> bool:
        return all([self.api_key, self.api_secret, self.api_passphrase])

    @property
    def can_trade(self) -> bool:
        return bool(self.private_key and self.funder)

    @property
    def has_proxy_config(self) -> bool:
        return any([self.http_proxy, self.https_proxy, self.no_proxy])

    def apply_proxy_environment(self) -> None:
        """把项目内的代理配置同步到标准 HTTP(S)_PROXY 环境变量。"""
        apply_proxy_environment_values(
            http_proxy=self.http_proxy,
            https_proxy=self.https_proxy,
            no_proxy=self.no_proxy,
        )

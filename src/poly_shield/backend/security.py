from __future__ import annotations

"""Local access security settings for the embedded web UI and Telegram bot."""

import os
from dataclasses import dataclass


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


def _env_float(name: str, *, default: float) -> float:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip()
    if not normalized:
        return default
    try:
        value = float(normalized)
    except ValueError as exc:
        raise RuntimeError(
            f"invalid {name}={raw!r}; expected a number") from exc
    if value <= 0:
        raise RuntimeError(
            f"invalid {name}={raw!r}; expected a value greater than zero")
    return value


def _env_int_frozenset(name: str) -> frozenset[int]:
    raw = os.getenv(name)
    if raw is None:
        return frozenset()
    values: set[int] = set()
    for part in raw.split(","):
        normalized = part.strip()
        if not normalized:
            continue
        try:
            values.add(int(normalized))
        except ValueError as exc:
            raise RuntimeError(
                f"invalid {name} entry {normalized!r}; expected comma-separated Telegram user IDs"
            ) from exc
    return frozenset(values)


@dataclass(frozen=True)
class LocalAccessSecuritySettings:
    """Security toggles for local UI/API access hardening."""

    ui_username: str = "admin"
    ui_password: str | None = None
    enforce_origin_check: bool = True
    csrf_cookie_name: str = "poly_csrf_token"
    csrf_header_name: str = "X-Poly-CSRF-Token"
    telegram_enabled: bool = False
    telegram_allowed_user_ids: frozenset[int] = frozenset()
    telegram_poll_interval_seconds: float = 5.0

    @property
    def requires_ui_auth(self) -> bool:
        return bool(self.ui_password)

    @property
    def telegram_whitelist_enabled(self) -> bool:
        return bool(self.telegram_allowed_user_ids)

    @classmethod
    def from_env(cls) -> "LocalAccessSecuritySettings":
        username = (os.getenv("POLY_UI_USERNAME")
                    or "admin").strip() or "admin"
        password = os.getenv("POLY_UI_PASSWORD")
        if password is not None:
            password = password.strip() or None
        return cls(
            ui_username=username,
            ui_password=password,
            enforce_origin_check=_env_bool(
                "POLY_ENFORCE_ORIGIN_CHECK", default=True),
            csrf_cookie_name=(os.getenv("POLY_CSRF_COOKIE_NAME")
                              or "poly_csrf_token").strip() or "poly_csrf_token",
            csrf_header_name=(os.getenv("POLY_CSRF_HEADER_NAME")
                              or "X-Poly-CSRF-Token").strip() or "X-Poly-CSRF-Token",
            telegram_enabled=_env_bool("POLY_TELEGRAM_ENABLED", default=False),
            telegram_allowed_user_ids=_env_int_frozenset(
                "POLY_TELEGRAM_ALLOWED_USER_IDS"
            ),
            telegram_poll_interval_seconds=_env_float(
                "POLY_TELEGRAM_POLL_INTERVAL_SECONDS", default=5.0
            ),
        )

from __future__ import annotations

"""Local access security settings for the embedded web UI."""

import os
from dataclasses import dataclass


def _env_bool(name: str, *, default: bool) -> bool:
    raw = os.getenv(name)
    if raw is None:
        return default
    normalized = raw.strip().lower()
    return normalized in {"1", "true", "yes", "on"}


@dataclass(frozen=True)
class LocalAccessSecuritySettings:
    """Security toggles for local UI/API access hardening."""

    ui_username: str = "admin"
    ui_password: str | None = None
    enforce_origin_check: bool = True
    csrf_cookie_name: str = "poly_csrf_token"
    csrf_header_name: str = "X-Poly-CSRF-Token"

    @property
    def requires_ui_auth(self) -> bool:
        return bool(self.ui_password)

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
        )

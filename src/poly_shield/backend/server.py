from __future__ import annotations

"""后端服务启动入口。"""

import argparse
import os
from pathlib import Path
from typing import Any, Sequence

import uvicorn

from poly_shield.backend.api import create_app
from poly_shield.backend.runtime import build_default_runtime
from poly_shield.backend.security import LocalAccessSecuritySettings
from poly_shield.backend.service import DEFAULT_DB_PATH, TaskService
from poly_shield.backend.telegram_bot import TelegramBotController, TelegramHttpTransport
from poly_shield.secret_store import LocalSecretStore
from poly_shield.wallet_identity import inspect_effective_signer, validate_signer_configuration


def _format_startup_banner(*, host: str, port: int, signer_info: dict[str, Any], validation: dict[str, Any] | None = None) -> str:
    lines = [
        "Poly Shield Local Server",
        f"Local URL: http://{host}:{port}",
    ]
    status = signer_info.get("status")
    if status == "valid":
        lines.extend([
            f"Signer: {signer_info.get('signer_address')}",
            f"Funder: {signer_info.get('configured_funder') or 'not set'}",
            f"Signature Type: {signer_info.get('signature_type') or 'not set'}",
            f"Proxy Wallet Mode: {'yes' if signer_info.get('proxy_wallet_mode') else 'no'}",
            f"Note: {signer_info.get('relationship_note')}",
        ])
    elif status == "missing":
        lines.append("Signer: not available (no effective private key found)")
    else:
        lines.append(
            f"Signer: unavailable ({signer_info.get('error', 'unknown error')})")

    if validation:
        lines.append(
            f"Signer Config Check: {'passed' if validation.get('ok') else 'failed'}")
        for warning in validation.get("warnings", []):
            lines.append(f"Warning: {warning}")
        for error in validation.get("errors", []):
            lines.append(f"Error: {error}")
    return "\n".join(lines)


def build_parser() -> argparse.ArgumentParser:
    """构建独立后端服务命令行。"""
    parser = argparse.ArgumentParser(
        prog="poly-shield-api", description="Poly Shield 后端服务")
    parser.add_argument("--host", default="127.0.0.1", help="监听地址")
    parser.add_argument("--port", type=int, default=8787, help="监听端口")
    parser.add_argument(
        "--db-path", default=str(DEFAULT_DB_PATH), help="SQLite 数据库路径")
    parser.add_argument(
        "--ui-username",
        default=os.getenv("POLY_UI_USERNAME", "admin"),
        help="本地 UI Basic Auth 用户名（默认 admin）",
    )
    parser.add_argument(
        "--ui-password",
        default=os.getenv("POLY_UI_PASSWORD"),
        help="本地 UI Basic Auth 密码；留空则不启用 UI 口令",
    )
    return parser


def main(argv: Sequence[str] | None = None) -> int:
    """启动 FastAPI 服务。"""
    parser = build_parser()
    args = parser.parse_args(argv)
    try:
        signer_info = inspect_effective_signer()
    except Exception as exc:
        signer_info = {
            "status": "error",
            "error": str(exc),
        }
    validation = validate_signer_configuration(signer_info)
    print(_format_startup_banner(host=args.host,
          port=args.port, signer_info=signer_info, validation=validation))
    if not validation.get("ok"):
        print("Startup aborted due to invalid signer/funder/signature_type configuration.")
        return 1
    service = TaskService.from_db_path(Path(args.db_path))
    runtime = build_default_runtime(service)
    env_security_settings = LocalAccessSecuritySettings.from_env()
    security_settings = LocalAccessSecuritySettings(
        ui_username=args.ui_username,
        ui_password=(args.ui_password.strip()
                     if args.ui_password and args.ui_password.strip() else None),
        enforce_origin_check=env_security_settings.enforce_origin_check,
        csrf_cookie_name=env_security_settings.csrf_cookie_name,
        csrf_header_name=env_security_settings.csrf_header_name,
        telegram_enabled=env_security_settings.telegram_enabled,
        telegram_allowed_user_ids=env_security_settings.telegram_allowed_user_ids,
        telegram_poll_interval_seconds=env_security_settings.telegram_poll_interval_seconds,
    )
    telegram_bot = None
    if security_settings.telegram_enabled:
        if not security_settings.telegram_whitelist_enabled:
            print(
                "Startup aborted because Telegram is enabled but POLY_TELEGRAM_ALLOWED_USER_IDS is empty.")
            return 1
        telegram_token = LocalSecretStore.default().load_telegram_bot_token()
        if not telegram_token:
            print(
                "Startup aborted because Telegram is enabled but no Telegram bot token is stored locally.")
            return 1
        telegram_bot = TelegramBotController(
            service=service,
            settings=security_settings,
            transport=TelegramHttpTransport(telegram_token),
            runtime_snapshot_provider=runtime.snapshot,
            refresh_runtime=runtime.refresh_active_tasks,
            notification_batch_size=50,
        )
    app = create_app(
        service,
        runtime=runtime,
        security_settings=security_settings,
        telegram_bot=telegram_bot,
    )
    uvicorn.run(app, host=args.host, port=args.port)
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(main())

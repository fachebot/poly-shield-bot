from __future__ import annotations

"""Helpers for resolving the effective signer identity without exposing the private key."""

import os
from typing import Any

from eth_utils import is_address

from poly_shield.secret_store import LocalSecretStore


def _parse_signature_type(value: str | None) -> tuple[int | None, str | None]:
    if value in {None, ""}:
        return None, None
    try:
        return int(value), None
    except ValueError:
        return None, f"invalid integer value: {value}"


def _derive_effective_user_address(
    *,
    signer_address: str | None,
    funder: str | None,
    signature_type: int | None,
) -> str | None:
    if signature_type in {1, 2}:
        return funder
    return signer_address or funder


def resolve_effective_private_key() -> tuple[str | None, str]:
    store = LocalSecretStore.default()
    private_key = store.load_private_key()
    if private_key:
        return private_key, "local-secret-store"
    return None, "missing"


def inspect_effective_signer() -> dict[str, Any]:
    from eth_account import Account

    private_key, source = resolve_effective_private_key()
    if not private_key:
        signature_type_raw = os.getenv("POLY_SIGNATURE_TYPE")
        signature_type, signature_type_parse_error = _parse_signature_type(
            signature_type_raw)
        funder = os.getenv("POLY_FUNDER") or os.getenv("FUNDER")
        effective_user_address = _derive_effective_user_address(
            signer_address=None,
            funder=funder,
            signature_type=signature_type,
        )
        return {
            "status": "missing",
            "source": source,
            "signer_address": None,
            "signature_type": signature_type_raw,
            "signature_type_raw": signature_type_raw,
            "signature_type_parse_error": signature_type_parse_error,
            "signature_type_value": signature_type,
            "proxy_wallet_mode": signature_type in {1, 2},
            "configured_funder": funder,
            "configured_user_address": effective_user_address,
            "effective_user_address": effective_user_address,
            "relationship_note": "no effective private key found",
        }

    account = Account.from_key(private_key)
    signer_address = account.address
    funder = os.getenv("POLY_FUNDER") or os.getenv("FUNDER")
    signature_type_raw = os.getenv("POLY_SIGNATURE_TYPE")
    signature_type, signature_type_parse_error = _parse_signature_type(
        signature_type_raw)
    proxy_wallet_mode = signature_type in {1, 2}
    effective_user_address = _derive_effective_user_address(
        signer_address=signer_address,
        funder=funder,
        signature_type=signature_type,
    )
    return {
        "status": "valid",
        "source": source,
        "signer_address": signer_address,
        "signature_type": None if signature_type is None else str(signature_type),
        "signature_type_raw": signature_type_raw,
        "signature_type_parse_error": signature_type_parse_error,
        "signature_type_value": signature_type,
        "proxy_wallet_mode": proxy_wallet_mode,
        "signer_matches_funder": None if not funder else signer_address.lower() == funder.lower(),
        "signer_matches_user_address": None if not effective_user_address else signer_address.lower() == effective_user_address.lower(),
        "configured_funder": funder,
        "configured_user_address": effective_user_address,
        "effective_user_address": effective_user_address,
        "relationship_note": (
            "signer and funder may differ in proxy-wallet mode"
            if proxy_wallet_mode
            else "signer usually matches funder in direct-wallet mode"
        ),
    }


def validate_signer_configuration(signer_info: dict[str, Any]) -> dict[str, Any]:
    """Validate signer/funder/signature_type combination before runtime starts."""
    errors: list[str] = []
    warnings: list[str] = []

    if signer_info.get("status") != "valid":
        errors.append(
            "missing effective private key: set private key in local secret-store")
        return {
            "ok": False,
            "errors": errors,
            "warnings": warnings,
        }

    signer_address = signer_info.get("signer_address")
    funder = signer_info.get("configured_funder")
    signature_type = signer_info.get("signature_type_value")
    proxy_wallet_mode = signer_info.get("proxy_wallet_mode") is True

    if signer_address and not is_address(str(signer_address)):
        errors.append(
            f"invalid signer address derived from private key: {signer_address}")

    if signer_info.get("signature_type_parse_error"):
        errors.append(
            f"unsupported POLY_SIGNATURE_TYPE={signer_info.get('signature_type_raw')}; expected integer 0, 1, or 2"
        )
    elif signature_type is not None and signature_type not in {0, 1, 2}:
        errors.append(
            f"unsupported POLY_SIGNATURE_TYPE={signature_type}; expected one of 0, 1, 2")

    if funder:
        if not is_address(str(funder)):
            errors.append(f"invalid funder address: {funder}")
    elif signature_type in {1, 2}:
        errors.append(
            "POLY_FUNDER is required when POLY_SIGNATURE_TYPE is 1 or 2")

    if funder and signer_address:
        signer_matches_funder = str(
            signer_address).lower() == str(funder).lower()
        if proxy_wallet_mode:
            if signer_matches_funder:
                warnings.append(
                    "proxy-wallet mode is enabled, but signer and funder are identical; verify this is intentional")
        elif not signer_matches_funder:
            errors.append(
                "signer/funder mismatch in direct-wallet mode; set POLY_SIGNATURE_TYPE=1/2 for proxy wallets or align funder with signer"
            )

    return {
        "ok": not errors,
        "errors": errors,
        "warnings": warnings,
    }

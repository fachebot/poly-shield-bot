from __future__ import annotations

"""Local encrypted secret storage with Windows DPAPI and Linux keyring support."""

import base64
import ctypes
import json
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from ctypes import wintypes
from typing import Any


_KEYRING_SERVICE_NAME = "poly-shield-bot"
_KEYRING_PRIVATE_KEY_ENTRY = "private-key"


def _is_windows() -> bool:
    return os.name == "nt"


def _is_linux() -> bool:
    return sys.platform.startswith("linux")


def _keyring_module() -> Any:
    try:
        import keyring
    except ImportError as exc:
        raise RuntimeError(
            "keyring package is required for Linux local encrypted secret storage"
        ) from exc
    return keyring


def _normalize_backend(value: str | None) -> str | None:
    if not value:
        return None
    normalized = value.strip().lower()
    if normalized in {"dpapi", "keyring"}:
        return normalized
    return normalized


def _default_secret_store_path() -> Path:
    configured = os.getenv("POLY_SECRET_STORE_PATH")
    if configured:
        return Path(configured)
    if _is_windows():
        local_app_data = os.getenv("LOCALAPPDATA")
        if local_app_data:
            return Path(local_app_data) / "PolyShield" / "secrets.json"
        return Path.home() / "AppData" / "Local" / "PolyShield" / "secrets.json"
    return Path.home() / ".poly-shield" / "secrets.json"


@dataclass(frozen=True)
class LocalSecretStore:
    """Persist selected local secrets in an encrypted file."""

    path: Path

    @property
    def backend(self) -> str:
        forced = _normalize_backend(os.getenv("POLY_SECRET_STORE_BACKEND"))
        if forced:
            return forced
        if _is_windows():
            return "dpapi"
        if _is_linux():
            return "keyring"
        return "dpapi"

    @classmethod
    def default(cls) -> "LocalSecretStore":
        return cls(_default_secret_store_path())

    def load_private_key(self) -> str | None:
        if self.backend == "keyring":
            return self._load_private_key_keyring()
        payload = self._read_payload()
        if payload is None:
            return None
        raw_secret = payload.get("private_key")
        if raw_secret is None:
            return None
        if not isinstance(raw_secret, dict):
            raise RuntimeError(
                "local secret store is malformed: private_key must be an object")
        scheme = raw_secret.get("scheme")
        ciphertext = raw_secret.get("ciphertext")
        if scheme != "dpapi":
            raise RuntimeError(
                f"unsupported local secret store scheme: {scheme}")
        if not isinstance(ciphertext, str) or not ciphertext:
            raise RuntimeError(
                "local secret store is malformed: ciphertext is missing")
        if not _is_windows():
            raise RuntimeError(
                "this local secret store was encrypted with Windows DPAPI and can only be read on Windows")
        decrypted = _unprotect_bytes_for_current_user(
            base64.urlsafe_b64decode(ciphertext.encode("ascii")))
        return decrypted.decode("utf-8")

    def save_private_key(self, private_key: str) -> Path:
        normalized = private_key.strip()
        if not normalized:
            raise ValueError("private key cannot be empty")
        if self.backend == "keyring":
            return self._save_private_key_keyring(normalized)
        if not _is_windows():
            raise RuntimeError(
                "local encrypted private-key storage currently supports Windows DPAPI only")
        payload = self._read_payload() or {"version": 1}
        payload["private_key"] = {
            "scheme": "dpapi",
            "ciphertext": base64.urlsafe_b64encode(
                _protect_bytes_for_current_user(normalized.encode("utf-8"))
            ).decode("ascii"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return self.path

    def clear_private_key(self) -> bool:
        if self.backend == "keyring":
            return self._clear_private_key_keyring()
        payload = self._read_payload()
        if payload is None or "private_key" not in payload:
            return False
        payload.pop("private_key", None)
        remaining_keys = {key for key in payload if key != "version"}
        if not remaining_keys:
            if self.path.exists():
                self.path.unlink()
            return True
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return True

    def has_private_key(self) -> bool:
        if self.backend == "keyring":
            keyring = _keyring_module()
            return bool(
                keyring.get_password(_KEYRING_SERVICE_NAME,
                                     _KEYRING_PRIVATE_KEY_ENTRY)
            )
        payload = self._read_payload()
        return bool(payload and payload.get("private_key"))

    def _load_private_key_keyring(self) -> str | None:
        keyring = _keyring_module()
        value = keyring.get_password(
            _KEYRING_SERVICE_NAME,
            _KEYRING_PRIVATE_KEY_ENTRY,
        )
        if value in {None, ""}:
            return None
        return str(value)

    def _save_private_key_keyring(self, private_key: str) -> Path:
        keyring = _keyring_module()
        keyring.set_password(
            _KEYRING_SERVICE_NAME,
            _KEYRING_PRIVATE_KEY_ENTRY,
            private_key,
        )
        payload = {
            "version": 1,
            "private_key": {
                "scheme": "keyring",
                "service": _KEYRING_SERVICE_NAME,
                "entry": _KEYRING_PRIVATE_KEY_ENTRY,
            },
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return self.path

    def _clear_private_key_keyring(self) -> bool:
        keyring = _keyring_module()
        had_value = bool(
            keyring.get_password(_KEYRING_SERVICE_NAME,
                                 _KEYRING_PRIVATE_KEY_ENTRY)
        )
        if had_value:
            keyring.delete_password(
                _KEYRING_SERVICE_NAME, _KEYRING_PRIVATE_KEY_ENTRY)
        payload = self._read_payload()
        if payload and "private_key" in payload:
            payload.pop("private_key", None)
            remaining_keys = {key for key in payload if key != "version"}
            if remaining_keys:
                self.path.write_text(json.dumps(
                    payload, indent=2), encoding="utf-8")
            elif self.path.exists():
                self.path.unlink()
        return had_value

    def _read_payload(self) -> dict[str, object] | None:
        if not self.path.exists():
            return None
        try:
            parsed = json.loads(self.path.read_text(encoding="utf-8"))
        except json.JSONDecodeError as exc:
            raise RuntimeError(
                f"failed to parse local secret store at {self.path}: {exc}") from exc
        if not isinstance(parsed, dict):
            raise RuntimeError(
                f"local secret store at {self.path} must be a JSON object")
        return parsed


class _DataBlob(ctypes.Structure):
    _fields_ = [
        ("cbData", wintypes.DWORD),
        ("pbData", ctypes.POINTER(ctypes.c_char)),
    ]


def _bytes_to_blob(raw: bytes) -> tuple[_DataBlob, ctypes.Array[ctypes.c_char]]:
    buffer = ctypes.create_string_buffer(raw)
    return _DataBlob(len(raw), ctypes.cast(buffer, ctypes.POINTER(ctypes.c_char))), buffer


def _blob_to_bytes(blob: _DataBlob) -> bytes:
    return ctypes.string_at(blob.pbData, blob.cbData)


def _protect_bytes_for_current_user(raw: bytes) -> bytes:
    if not _is_windows():
        raise RuntimeError("Windows DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, in_buffer = _bytes_to_blob(raw)
    out_blob = _DataBlob()
    description = "Poly Shield Private Key"
    if not crypt32.CryptProtectData(
        ctypes.byref(in_blob),
        description,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise OSError(ctypes.get_last_error(), "CryptProtectData failed")
    try:
        return _blob_to_bytes(out_blob)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)
        del in_buffer


def _unprotect_bytes_for_current_user(raw: bytes) -> bytes:
    if not _is_windows():
        raise RuntimeError("Windows DPAPI is only available on Windows")
    crypt32 = ctypes.windll.crypt32
    kernel32 = ctypes.windll.kernel32
    in_blob, in_buffer = _bytes_to_blob(raw)
    out_blob = _DataBlob()
    if not crypt32.CryptUnprotectData(
        ctypes.byref(in_blob),
        None,
        None,
        None,
        None,
        0,
        ctypes.byref(out_blob),
    ):
        raise OSError(ctypes.get_last_error(), "CryptUnprotectData failed")
    try:
        return _blob_to_bytes(out_blob)
    finally:
        if out_blob.pbData:
            kernel32.LocalFree(out_blob.pbData)
        del in_buffer

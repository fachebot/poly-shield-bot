from __future__ import annotations

"""Local encrypted secret storage with Windows DPAPI and Linux keyring support."""

import base64
import ctypes
import json
import os
import subprocess
import sys
import tempfile
from dataclasses import dataclass
from pathlib import Path
from ctypes import wintypes
from typing import Any


_KEYRING_SERVICE_NAME = "poly-shield-bot"
_KEYRING_PRIVATE_KEY_ENTRY = "private-key"
_KEYRING_TELEGRAM_BOT_TOKEN_ENTRY = "telegram-bot-token"
_TPM2_REQUIRED_COMMANDS = (
    "tpm2_createprimary",
    "tpm2_create",
    "tpm2_load",
    "tpm2_unseal",
)


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
    if normalized in {"dpapi", "keyring", "tpm2"}:
        return normalized
    return normalized


def _require_supported_backend(value: str) -> str:
    if value in {"dpapi", "keyring", "tpm2"}:
        return value
    raise RuntimeError(
        f"unsupported POLY_SECRET_STORE_BACKEND={value}; expected one of dpapi, keyring, tpm2"
    )


def _tpm2_command_path(command: str) -> str | None:
    from shutil import which

    return which(command)


def _require_tpm2_commands() -> None:
    missing = [
        cmd for cmd in _TPM2_REQUIRED_COMMANDS if _tpm2_command_path(cmd) is None]
    if missing:
        missing_hint = ", ".join(missing)
        raise RuntimeError(
            "tpm2 backend requires tpm2-tools in PATH; missing commands: "
            f"{missing_hint}. install package: tpm2-tools"
        )


def _run_tpm2_command(args: list[str]) -> bytes:
    result = subprocess.run(args, check=False, capture_output=True)
    if result.returncode != 0:
        stderr = result.stderr.decode("utf-8", errors="replace").strip()
        stdout = result.stdout.decode("utf-8", errors="replace").strip()
        details = stderr or stdout or "unknown error"
        raise RuntimeError(
            f"TPM command failed: {' '.join(args)} :: {details}")
    return result.stdout


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
            return _require_supported_backend(forced)
        if _is_windows():
            return "dpapi"
        if _is_linux():
            return "tpm2"
        return "dpapi"

    @classmethod
    def default(cls) -> "LocalSecretStore":
        return cls(_default_secret_store_path())

    def load_private_key(self) -> str | None:
        if self.backend == "tpm2":
            return self._load_secret_tpm2("private_key")
        if self.backend == "keyring":
            return self._load_secret_keyring(
                secret_name="private_key",
                keyring_entry=_KEYRING_PRIVATE_KEY_ENTRY,
            )
        return self._load_secret_dpapi("private_key")

    def save_private_key(self, private_key: str) -> Path:
        normalized = private_key.strip()
        if not normalized:
            raise ValueError("private key cannot be empty")
        if self.backend == "tpm2":
            return self._save_secret_tpm2("private_key", normalized)
        if self.backend == "keyring":
            return self._save_secret_keyring(
                secret_name="private_key",
                keyring_entry=_KEYRING_PRIVATE_KEY_ENTRY,
                value=normalized,
            )
        return self._save_secret_dpapi("private_key", normalized)

    def clear_private_key(self) -> bool:
        if self.backend == "tpm2":
            return self._clear_secret_tpm2("private_key")
        if self.backend == "keyring":
            return self._clear_secret_keyring(
                secret_name="private_key",
                keyring_entry=_KEYRING_PRIVATE_KEY_ENTRY,
            )
        return self._clear_secret_payload_entry("private_key")

    def has_private_key(self) -> bool:
        return self._has_named_secret("private_key")

    def load_telegram_bot_token(self) -> str | None:
        if self.backend == "tpm2":
            return self._load_secret_tpm2("telegram_bot_token")
        if self.backend == "keyring":
            return self._load_secret_keyring(
                secret_name="telegram_bot_token",
                keyring_entry=_KEYRING_TELEGRAM_BOT_TOKEN_ENTRY,
            )
        return self._load_secret_dpapi("telegram_bot_token")

    def save_telegram_bot_token(self, token: str) -> Path:
        normalized = token.strip()
        if not normalized:
            raise ValueError("telegram bot token cannot be empty")
        if self.backend == "tpm2":
            return self._save_secret_tpm2("telegram_bot_token", normalized)
        if self.backend == "keyring":
            return self._save_secret_keyring(
                secret_name="telegram_bot_token",
                keyring_entry=_KEYRING_TELEGRAM_BOT_TOKEN_ENTRY,
                value=normalized,
            )
        return self._save_secret_dpapi("telegram_bot_token", normalized)

    def clear_telegram_bot_token(self) -> bool:
        if self.backend == "tpm2":
            return self._clear_secret_tpm2("telegram_bot_token")
        if self.backend == "keyring":
            return self._clear_secret_keyring(
                secret_name="telegram_bot_token",
                keyring_entry=_KEYRING_TELEGRAM_BOT_TOKEN_ENTRY,
            )
        return self._clear_secret_payload_entry("telegram_bot_token")

    def has_telegram_bot_token(self) -> bool:
        return self._has_named_secret("telegram_bot_token")

    def _has_named_secret(self, secret_name: str) -> bool:
        if self.backend == "tpm2":
            payload = self._read_payload()
            if not payload:
                return False
            secret = payload.get(secret_name)
            return bool(
                isinstance(secret, dict)
                and secret.get("scheme") == "tpm2"
                and secret.get("public")
                and secret.get("private")
            )
        if self.backend == "keyring":
            entry_name = _KEYRING_PRIVATE_KEY_ENTRY
            if secret_name == "telegram_bot_token":
                entry_name = _KEYRING_TELEGRAM_BOT_TOKEN_ENTRY
            keyring = _keyring_module()
            return bool(keyring.get_password(_KEYRING_SERVICE_NAME, entry_name))
        payload = self._read_payload()
        return bool(payload and payload.get(secret_name))

    def _load_secret_dpapi(self, secret_name: str) -> str | None:
        payload = self._read_payload()
        if payload is None:
            return None
        raw_secret = payload.get(secret_name)
        if raw_secret is None:
            return None
        if not isinstance(raw_secret, dict):
            raise RuntimeError(
                f"local secret store is malformed: {secret_name} must be an object")
        scheme = raw_secret.get("scheme")
        ciphertext = raw_secret.get("ciphertext")
        if scheme != "dpapi":
            raise RuntimeError(
                f"unsupported local secret store scheme for {secret_name}: {scheme}")
        if not isinstance(ciphertext, str) or not ciphertext:
            raise RuntimeError(
                f"local secret store is malformed: {secret_name} ciphertext is missing")
        if not _is_windows():
            raise RuntimeError(
                "this local secret store was encrypted with Windows DPAPI and can only be read on Windows")
        decrypted = _unprotect_bytes_for_current_user(
            base64.urlsafe_b64decode(ciphertext.encode("ascii")))
        return decrypted.decode("utf-8")

    def _save_secret_dpapi(self, secret_name: str, value: str) -> Path:
        if not _is_windows():
            raise RuntimeError(
                "local encrypted secret storage currently supports Windows DPAPI only"
            )
        payload = self._read_payload() or {"version": 1}
        payload[secret_name] = {
            "scheme": "dpapi",
            "ciphertext": base64.urlsafe_b64encode(
                _protect_bytes_for_current_user(value.encode("utf-8"))
            ).decode("ascii"),
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return self.path

    def _clear_secret_payload_entry(self, secret_name: str) -> bool:
        payload = self._read_payload()
        if payload is None or secret_name not in payload:
            return False
        payload.pop(secret_name, None)
        remaining_keys = {key for key in payload if key != "version"}
        if not remaining_keys:
            if self.path.exists():
                self.path.unlink()
            return True
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return True

    def _load_secret_tpm2(self, secret_name: str) -> str | None:
        if not _is_linux():
            raise RuntimeError("tpm2 backend is only supported on Linux")
        _require_tpm2_commands()
        payload = self._read_payload()
        if payload is None:
            return None
        raw_secret = payload.get(secret_name)
        if raw_secret is None:
            return None
        if not isinstance(raw_secret, dict):
            raise RuntimeError(
                f"local secret store is malformed: {secret_name} must be an object")
        if raw_secret.get("scheme") != "tpm2":
            raise RuntimeError(
                f"unsupported local secret store scheme for {secret_name}: {raw_secret.get('scheme')}")
        public_blob = raw_secret.get("public")
        private_blob = raw_secret.get("private")
        if not isinstance(public_blob, str) or not isinstance(private_blob, str):
            raise RuntimeError(
                f"local secret store is malformed: {secret_name} tpm2 public/private blobs are missing")

        with tempfile.TemporaryDirectory(prefix="poly-shield-tpm2-") as temp_dir:
            temp_path = Path(temp_dir)
            primary_ctx = temp_path / "primary.ctx"
            sealed_pub = temp_path / "sealed.pub"
            sealed_priv = temp_path / "sealed.priv"
            sealed_ctx = temp_path / "sealed.ctx"

            sealed_pub.write_bytes(
                base64.urlsafe_b64decode(public_blob.encode("ascii")))
            sealed_priv.write_bytes(
                base64.urlsafe_b64decode(private_blob.encode("ascii")))

            _run_tpm2_command([
                "tpm2_createprimary",
                "-Q",
                "-C",
                "o",
                "-g",
                "sha256",
                "-G",
                "rsa",
                "-c",
                str(primary_ctx),
            ])
            _run_tpm2_command([
                "tpm2_load",
                "-Q",
                "-C",
                str(primary_ctx),
                "-u",
                str(sealed_pub),
                "-r",
                str(sealed_priv),
                "-c",
                str(sealed_ctx),
            ])
            unsealed = _run_tpm2_command([
                "tpm2_unseal",
                "-Q",
                "-c",
                str(sealed_ctx),
            ])
            return unsealed.decode("utf-8").strip()

    def _save_secret_tpm2(self, secret_name: str, value: str) -> Path:
        if not _is_linux():
            raise RuntimeError("tpm2 backend is only supported on Linux")
        _require_tpm2_commands()

        with tempfile.TemporaryDirectory(prefix="poly-shield-tpm2-") as temp_dir:
            temp_path = Path(temp_dir)
            secret_file = temp_path / "secret.txt"
            primary_ctx = temp_path / "primary.ctx"
            sealed_pub = temp_path / "sealed.pub"
            sealed_priv = temp_path / "sealed.priv"

            secret_file.write_text(value, encoding="utf-8")

            _run_tpm2_command([
                "tpm2_createprimary",
                "-Q",
                "-C",
                "o",
                "-g",
                "sha256",
                "-G",
                "rsa",
                "-c",
                str(primary_ctx),
            ])
            _run_tpm2_command([
                "tpm2_create",
                "-Q",
                "-C",
                str(primary_ctx),
                "-u",
                str(sealed_pub),
                "-r",
                str(sealed_priv),
                "-i",
                str(secret_file),
            ])

            payload = self._read_payload() or {"version": 1}
            payload[secret_name] = {
                "scheme": "tpm2",
                "public": base64.urlsafe_b64encode(
                    sealed_pub.read_bytes()).decode("ascii"),
                "private": base64.urlsafe_b64encode(
                    sealed_priv.read_bytes()).decode("ascii"),
                "primary_hierarchy": "owner",
                "primary_alg": "rsa",
            }
            self.path.parent.mkdir(parents=True, exist_ok=True)
            self.path.write_text(json.dumps(
                payload, indent=2), encoding="utf-8")
            return self.path

    def _clear_secret_tpm2(self, secret_name: str) -> bool:
        return self._clear_secret_payload_entry(secret_name)

    def _load_secret_keyring(self, *, secret_name: str, keyring_entry: str) -> str | None:
        keyring = _keyring_module()
        value = keyring.get_password(
            _KEYRING_SERVICE_NAME,
            keyring_entry,
        )
        if value in {None, ""}:
            return None
        return str(value)

    def _save_secret_keyring(self, *, secret_name: str, keyring_entry: str, value: str) -> Path:
        keyring = _keyring_module()
        keyring.set_password(
            _KEYRING_SERVICE_NAME,
            keyring_entry,
            value,
        )
        payload = self._read_payload() or {"version": 1}
        payload[secret_name] = {
            "scheme": "keyring",
            "service": _KEYRING_SERVICE_NAME,
            "entry": keyring_entry,
        }
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps(payload, indent=2), encoding="utf-8")
        return self.path

    def _clear_secret_keyring(self, *, secret_name: str, keyring_entry: str) -> bool:
        keyring = _keyring_module()
        had_value = bool(keyring.get_password(
            _KEYRING_SERVICE_NAME, keyring_entry))
        if had_value:
            keyring.delete_password(_KEYRING_SERVICE_NAME, keyring_entry)
        payload = self._read_payload()
        if payload and secret_name in payload:
            payload.pop(secret_name, None)
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

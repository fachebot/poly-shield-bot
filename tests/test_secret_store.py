import json

from poly_shield.config import PolymarketCredentials
from poly_shield.secret_store import LocalSecretStore


def test_local_secret_store_round_trip_uses_dpapi_backend(tmp_path, monkeypatch) -> None:
    store = LocalSecretStore(tmp_path / "secrets.json")

    monkeypatch.setattr("poly_shield.secret_store._is_windows", lambda: True)
    monkeypatch.setattr(
        "poly_shield.secret_store._protect_bytes_for_current_user",
        lambda raw: b"protected:" + raw,
    )
    monkeypatch.setattr(
        "poly_shield.secret_store._unprotect_bytes_for_current_user",
        lambda raw: raw.removeprefix(b"protected:"),
    )

    path = store.save_private_key("0xabc123")

    assert path.exists()
    assert store.load_private_key() == "0xabc123"
    assert store.has_private_key() is True

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["private_key"]["scheme"] == "dpapi"


def test_local_secret_store_clear_private_key_removes_file(tmp_path, monkeypatch) -> None:
    store = LocalSecretStore(tmp_path / "secrets.json")

    monkeypatch.setattr("poly_shield.secret_store._is_windows", lambda: True)
    monkeypatch.setattr(
        "poly_shield.secret_store._protect_bytes_for_current_user",
        lambda raw: raw,
    )

    store.save_private_key("0xdef456")

    assert store.clear_private_key() is True
    assert not store.path.exists()
    assert store.has_private_key() is False


def test_credentials_from_env_falls_back_to_local_secret_store(monkeypatch) -> None:
    for name in [
        "POLY_PRIVATE_KEY",
        "PK",
        "POLY_API_KEY",
        "CLOB_API_KEY",
        "POLY_API_SECRET",
        "CLOB_SECRET",
        "POLY_API_PASSPHRASE",
        "CLOB_PASS_PHRASE",
        "POLY_FUNDER",
        "FUNDER",
        "POLY_SIGNATURE_TYPE",
    ]:
        monkeypatch.delenv(name, raising=False)

    class FakeStore:
        def load_private_key(self) -> str:
            return "0xfrom-store"

    monkeypatch.setattr(
        "poly_shield.config.LocalSecretStore.default", lambda: FakeStore())

    credentials = PolymarketCredentials.from_env()

    assert credentials.private_key == "0xfrom-store"


def test_credentials_ignores_env_private_key_when_store_is_available(monkeypatch) -> None:
    monkeypatch.setenv("POLY_PRIVATE_KEY", "0xfrom-env")
    monkeypatch.setenv("PK", "0xfrom-env-alias")

    class FakeStore:
        def load_private_key(self) -> str:
            return "0xfrom-store"

    monkeypatch.setattr(
        "poly_shield.config.LocalSecretStore.default", lambda: FakeStore())

    credentials = PolymarketCredentials.from_env()

    assert credentials.private_key == "0xfrom-store"


def test_credentials_derives_user_address_from_signer_in_direct_mode(monkeypatch) -> None:
    class FakeStore:
        def load_private_key(self) -> str:
            return "0x" + "11" * 32

    monkeypatch.setattr(
        "poly_shield.config.LocalSecretStore.default", lambda: FakeStore())
    monkeypatch.delenv("POLY_SIGNATURE_TYPE", raising=False)
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    monkeypatch.delenv("FUNDER", raising=False)

    credentials = PolymarketCredentials.from_env()

    assert credentials.user_address == "0x19E7E376E7C213B7E7e7e46cc70A5dD086DAff2A"


def test_credentials_derives_user_address_from_funder_in_proxy_mode(monkeypatch) -> None:
    class FakeStore:
        def load_private_key(self) -> str:
            return "0x" + "11" * 32

    monkeypatch.setattr(
        "poly_shield.config.LocalSecretStore.default", lambda: FakeStore())
    monkeypatch.setenv("POLY_SIGNATURE_TYPE", "1")
    monkeypatch.setenv(
        "POLY_FUNDER", "0x2222222222222222222222222222222222222222")

    credentials = PolymarketCredentials.from_env()

    assert credentials.user_address == "0x2222222222222222222222222222222222222222"


def test_local_secret_store_round_trip_uses_keyring_backend(tmp_path, monkeypatch) -> None:
    store = LocalSecretStore(tmp_path / "secrets.json")
    state: dict[tuple[str, str], str] = {}
    monkeypatch.setenv("POLY_SECRET_STORE_BACKEND", "keyring")

    class FakeKeyring:
        @staticmethod
        def get_password(service: str, entry: str):
            return state.get((service, entry))

        @staticmethod
        def set_password(service: str, entry: str, value: str) -> None:
            state[(service, entry)] = value

        @staticmethod
        def delete_password(service: str, entry: str) -> None:
            state.pop((service, entry), None)

    monkeypatch.setattr("poly_shield.secret_store._is_windows", lambda: False)
    monkeypatch.setattr("poly_shield.secret_store._is_linux", lambda: True)
    monkeypatch.setattr(
        "poly_shield.secret_store._keyring_module", lambda: FakeKeyring)

    path = store.save_private_key("0xkeyring")

    assert path.exists()
    assert store.load_private_key() == "0xkeyring"
    assert store.has_private_key() is True

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["private_key"]["scheme"] == "keyring"


def test_local_secret_store_clear_private_key_keyring_backend(tmp_path, monkeypatch) -> None:
    store = LocalSecretStore(tmp_path / "secrets.json")
    state: dict[tuple[str, str], str] = {}
    monkeypatch.setenv("POLY_SECRET_STORE_BACKEND", "keyring")

    class FakeKeyring:
        @staticmethod
        def get_password(service: str, entry: str):
            return state.get((service, entry))

        @staticmethod
        def set_password(service: str, entry: str, value: str) -> None:
            state[(service, entry)] = value

        @staticmethod
        def delete_password(service: str, entry: str) -> None:
            state.pop((service, entry), None)

    monkeypatch.setattr("poly_shield.secret_store._is_windows", lambda: False)
    monkeypatch.setattr("poly_shield.secret_store._is_linux", lambda: True)
    monkeypatch.setattr(
        "poly_shield.secret_store._keyring_module", lambda: FakeKeyring)

    store.save_private_key("0xkeyring")

    assert store.clear_private_key() is True
    assert store.has_private_key() is False
    assert store.path.exists() is False


def test_local_secret_store_uses_tpm2_backend_by_default_on_linux(monkeypatch, tmp_path) -> None:
    store = LocalSecretStore(tmp_path / "secrets.json")
    monkeypatch.delenv("POLY_SECRET_STORE_BACKEND", raising=False)
    monkeypatch.setattr("poly_shield.secret_store._is_windows", lambda: False)
    monkeypatch.setattr("poly_shield.secret_store._is_linux", lambda: True)

    assert store.backend == "tpm2"


def test_local_secret_store_routes_to_tpm2_backend(monkeypatch, tmp_path) -> None:
    store = LocalSecretStore(tmp_path / "secrets.json")
    called: dict[str, object] = {}

    monkeypatch.setenv("POLY_SECRET_STORE_BACKEND", "tpm2")
    monkeypatch.setattr("poly_shield.secret_store._is_windows", lambda: False)
    monkeypatch.setattr("poly_shield.secret_store._is_linux", lambda: True)

    def fake_save(self, private_key: str):
        called["save"] = private_key
        return self.path

    def fake_load(self):
        called["load"] = True
        return "0xtpm2"

    def fake_clear(self):
        called["clear"] = True
        return True

    monkeypatch.setattr(LocalSecretStore, "_save_private_key_tpm2", fake_save)
    monkeypatch.setattr(LocalSecretStore, "_load_private_key_tpm2", fake_load)
    monkeypatch.setattr(
        LocalSecretStore, "_clear_private_key_tpm2", fake_clear)

    assert store.save_private_key("0xabc") == store.path
    assert store.load_private_key() == "0xtpm2"
    assert store.clear_private_key() is True
    assert called == {
        "save": "0xabc",
        "load": True,
        "clear": True,
    }

from poly_shield.wallet_identity import inspect_effective_signer, validate_signer_configuration


def test_validate_signer_configuration_rejects_direct_mode_mismatch() -> None:
    signer_info = {
        "status": "valid",
        "signer_address": "0x1111111111111111111111111111111111111111",
        "configured_funder": "0x2222222222222222222222222222222222222222",
        "signature_type_value": 0,
        "proxy_wallet_mode": False,
    }

    result = validate_signer_configuration(signer_info)

    assert result["ok"] is False
    assert any(
        "signer/funder mismatch in direct-wallet mode" in item for item in result["errors"])


def test_validate_signer_configuration_allows_proxy_mode_mismatch() -> None:
    signer_info = {
        "status": "valid",
        "signer_address": "0x1111111111111111111111111111111111111111",
        "configured_funder": "0x2222222222222222222222222222222222222222",
        "signature_type_value": 1,
        "proxy_wallet_mode": True,
    }

    result = validate_signer_configuration(signer_info)

    assert result["ok"] is True
    assert result["errors"] == []


def test_inspect_effective_signer_derives_user_address_from_signer_in_direct_mode(monkeypatch) -> None:
    class FakeStore:
        def load_private_key(self) -> str:
            return "0x" + "11" * 32

    monkeypatch.setattr(
        "poly_shield.wallet_identity.LocalSecretStore.default", lambda: FakeStore())
    monkeypatch.delenv("POLY_SIGNATURE_TYPE", raising=False)
    monkeypatch.delenv("POLY_FUNDER", raising=False)
    monkeypatch.delenv("FUNDER", raising=False)

    payload = inspect_effective_signer()

    assert payload["status"] == "valid"
    assert payload["effective_user_address"] == payload["signer_address"]


def test_inspect_effective_signer_derives_user_address_from_funder_in_proxy_mode(monkeypatch) -> None:
    class FakeStore:
        def load_private_key(self) -> str:
            return "0x" + "11" * 32

    monkeypatch.setattr(
        "poly_shield.wallet_identity.LocalSecretStore.default", lambda: FakeStore())
    monkeypatch.setenv("POLY_SIGNATURE_TYPE", "1")
    monkeypatch.setenv(
        "POLY_FUNDER", "0x2222222222222222222222222222222222222222")

    payload = inspect_effective_signer()

    assert payload["status"] == "valid"
    assert payload["effective_user_address"] == "0x2222222222222222222222222222222222222222"

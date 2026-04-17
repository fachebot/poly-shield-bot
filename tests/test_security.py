from poly_shield.backend.api import create_app
from poly_shield.backend.security import LocalAccessSecuritySettings
from poly_shield.backend.service import TaskService

from fastapi.testclient import TestClient


def test_local_access_security_settings_reads_telegram_env(monkeypatch) -> None:
    monkeypatch.setenv("POLY_TELEGRAM_ENABLED", "true")
    monkeypatch.setenv("POLY_TELEGRAM_ALLOWED_USER_IDS", "101, 202 ,303")
    monkeypatch.setenv("POLY_TELEGRAM_POLL_INTERVAL_SECONDS", "2.5")

    settings = LocalAccessSecuritySettings.from_env()

    assert settings.telegram_enabled is True
    assert settings.telegram_allowed_user_ids == frozenset({101, 202, 303})
    assert settings.telegram_whitelist_enabled is True
    assert settings.telegram_poll_interval_seconds == 2.5


def test_local_access_security_settings_rejects_invalid_telegram_user_ids(monkeypatch) -> None:
    monkeypatch.setenv("POLY_TELEGRAM_ALLOWED_USER_IDS", "101,nope")

    try:
        LocalAccessSecuritySettings.from_env()
    except RuntimeError as exc:
        assert "POLY_TELEGRAM_ALLOWED_USER_IDS" in str(exc)
    else:  # pragma: no cover
        raise AssertionError(
            "expected invalid Telegram user IDs to be rejected")


def test_health_reports_non_sensitive_telegram_security_flags(tmp_path) -> None:
    service = TaskService.from_db_path(tmp_path / "poly-shield.db")
    settings = LocalAccessSecuritySettings(
        telegram_enabled=True,
        telegram_allowed_user_ids=frozenset({101, 202}),
        telegram_poll_interval_seconds=3.0,
    )

    client = TestClient(create_app(service, security_settings=settings))
    response = client.get("/health")

    assert response.status_code == 200
    assert response.json()["local_security"]["telegram_enabled"] is True
    assert response.json()[
        "local_security"]["telegram_whitelist_enabled"] is True
    assert response.json()["local_security"]["telegram_whitelist_size"] == 2
    assert response.json()[
        "local_security"]["telegram_poll_interval_seconds"] == 3.0

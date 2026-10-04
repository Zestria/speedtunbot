"""Tests for :mod:`app.settings`."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from app.settings import DEFAULT_DATABASE_URL, DEFAULT_TIMEZONE, Settings, get_settings

_REQUIRED = {
    "bot_token": "123456:AA-test-token",
    "vpn_token": "vpn-token-value",
    "domain": "https://panel.example.com:2053/",
    "sub_url_base": "https://panel.example.com:2053/sub/",
    "inbound_id": 1,
}


def _make(**overrides: object) -> Settings:
    # ``_env_file=None`` keeps the tests independent of the real ``.env``.
    return Settings(_env_file=None, **{**_REQUIRED, **overrides})


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("[1,2]", [1, 2]),
        ("1,2", [1, 2]),
        ("1", [1]),
        (" 1 , 2 ", [1, 2]),
        ([1, 2], [1, 2]),
    ],
)
def test_owner_ids_parsing(raw: object, expected: list[int]) -> None:
    settings = _make(owner_ids=raw)
    assert settings.owner_ids == expected


def test_owner_ids_empty_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(owner_ids="")


def test_owner_ids_falls_back_to_legacy_admin_ids(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.WARNING, logger="app.settings"):
        settings = _make(admin_ids="[42]")
    assert settings.owner_ids == [42]
    assert any("ADMIN_IDS" in record.message for record in caplog.records)


def test_missing_required_field_names_the_field() -> None:
    payload = dict(_REQUIRED)
    payload.pop("vpn_token")
    with pytest.raises(ValidationError) as excinfo:
        Settings(_env_file=None, **payload)
    assert "vpn_token" in str(excinfo.value)


def test_defaults_are_applied() -> None:
    settings = _make(owner_ids="1")
    assert settings.database_url == DEFAULT_DATABASE_URL
    assert settings.timezone == DEFAULT_TIMEZONE
    assert settings.digest_hour == 9
    assert settings.log_level == "INFO"
    assert settings.auto_import_legacy is False


def test_invalid_timezone_is_rejected() -> None:
    with pytest.raises(ValidationError):
        _make(owner_ids="1", timezone="Mars/Olympus")


def test_localhost_sub_url_base_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.settings"):
        _make(owner_ids="1", sub_url_base="https://127.0.0.1:2053/sub/")
    assert any("SUB_URL_BASE" in record.message for record in caplog.records)


def test_malformed_sub_url_base_warns(caplog: pytest.LogCaptureFixture) -> None:
    with caplog.at_level(logging.WARNING, logger="app.settings"):
        _make(owner_ids="1", sub_url_base="not a url")
    assert any("SUB_URL_BASE" in record.message for record in caplog.records)


def test_get_settings_is_cached(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("BOT_TOKEN", "123456:AA-test-token")
    monkeypatch.setenv("VPN_TOKEN", "vpn-token-value")
    monkeypatch.setenv("DOMAIN", "https://panel.example.com:2053/")
    monkeypatch.setenv("SUB_URL_BASE", "https://panel.example.com:2053/sub/")
    monkeypatch.setenv("INBOUND_ID", "1")
    monkeypatch.setenv("OWNER_IDS", "[1]")
    get_settings.cache_clear()
    try:
        assert get_settings() is get_settings()
    finally:
        get_settings.cache_clear()

"""Environment-backed application configuration.

All values come from environment variables (optionally via a ``.env`` file) and
are validated at construction time, so a bad configuration produces a readable
``pydantic.ValidationError`` instead of a raw traceback
(``TASK_PLAN.md`` §2.3 / §M0-01).
"""

from __future__ import annotations

import json
import logging
from functools import lru_cache
from typing import Any
from urllib.parse import urlparse
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from pydantic import field_validator, model_validator
from pydantic_settings import BaseSettings, SettingsConfigDict

logger = logging.getLogger(__name__)

DEFAULT_DATABASE_URL = "sqlite+aiosqlite:///data/bot.db"
DEFAULT_TIMEZONE = "UTC"
DEFAULT_BANNED_USERS_FILE = "banned_users.json"

_LOCAL_HOSTS = frozenset({"localhost", "127.0.0.1", "::1", "0.0.0.0"})


def _parse_id_list(value: Any) -> list[int]:
    """Parse a Telegram ID list from a JSON list or a comma-separated string."""
    if value is None or value == "":
        return []
    if isinstance(value, (list, tuple, set, frozenset)):
        return [int(item) for item in value]
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return []
        if text.startswith("["):
            try:
                parsed = json.loads(text)
            except json.JSONDecodeError as exc:
                raise ValueError(f"invalid JSON list: {exc}") from exc
            if not isinstance(parsed, list):
                raise ValueError("expected a JSON list of Telegram IDs")
            return [int(item) for item in parsed]
        return [int(part.strip()) for part in text.split(",") if part.strip()]
    raise ValueError(f"cannot parse a list of Telegram IDs from {value!r}")


class Settings(BaseSettings):
    """Validated application settings."""

    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
        extra="ignore",
    )

    # --- Required (as today) ---
    bot_token: str
    vpn_token: str
    domain: str
    sub_url_base: str
    inbound_id: int

    # --- Access / roles ---
    owner_ids: list[int] = []
    # Legacy fallback: deprecated in favour of OWNER_IDS.
    admin_ids: list[int] = []

    # --- Infrastructure ---
    database_url: str = DEFAULT_DATABASE_URL
    timezone: str = DEFAULT_TIMEZONE
    digest_hour: int = 9
    log_level: str = "INFO"
    auto_import_legacy: bool = False

    # --- Legacy seed sources (DB becomes authoritative after M0-07 / M0-10) ---
    bank_account_details: str | None = None
    banned_users_file: str = DEFAULT_BANNED_USERS_FILE

    @field_validator("owner_ids", "admin_ids", mode="before")
    @classmethod
    def _validate_id_list(cls, value: Any) -> list[int]:
        return _parse_id_list(value)

    @field_validator("timezone")
    @classmethod
    def _validate_timezone(cls, value: str) -> str:
        try:
            ZoneInfo(value)
        except (ZoneInfoNotFoundError, ValueError) as exc:
            raise ValueError(f"unknown timezone: {value!r}") from exc
        return value

    @model_validator(mode="after")
    def _resolve_owners(self) -> Settings:
        if not self.owner_ids and self.admin_ids:
            logger.warning(
                "OWNER_IDS is not set; falling back to the deprecated "
                "ADMIN_IDS variable. Rename ADMIN_IDS to OWNER_IDS."
            )
            self.owner_ids = list(self.admin_ids)
        if not self.owner_ids:
            raise ValueError("OWNER_IDS must contain at least one Telegram ID")
        return self

    @model_validator(mode="after")
    def _warn_sub_url_base(self) -> Settings:
        parsed = urlparse(self.sub_url_base)
        host = parsed.hostname
        if not parsed.scheme or not host:
            logger.warning(
                "SUB_URL_BASE %r is not a valid URL; subscription links may be broken.",
                self.sub_url_base,
            )
        elif host in _LOCAL_HOSTS:
            logger.warning(
                "SUB_URL_BASE host is %s; subscription links will not work "
                "outside this machine.",
                host,
            )
        return self


@lru_cache
def get_settings() -> Settings:
    """Return the process-wide, cached settings instance."""
    return Settings()

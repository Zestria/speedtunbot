"""Logging configuration with secret masking.

Every ``LogRecord`` passes through :class:`SecretMaskingFilter`, which replaces
known secret values (``BOT_TOKEN`` / ``VPN_TOKEN``) with ``***`` before the
record is formatted. Secrets must never reach stdout, files, or alert messages
(``TASK_PLAN.md`` §0.1 rule 6).
"""

from __future__ import annotations

import logging
import os
import sys
from collections.abc import Iterable

MASK = "***"

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


class SecretMaskingFilter(logging.Filter):
    """Replace secret substrings in log messages and arguments."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        # Ignore short/falsy values to avoid over-masking ordinary text.
        self._secrets = tuple(s for s in secrets if s and len(s) >= 8)

    def _mask(self, value: str) -> str:
        for secret in self._secrets:
            if secret in value:
                value = value.replace(secret, MASK)
        return value

    def filter(self, record: logging.LogRecord) -> bool:
        if self._secrets:
            if isinstance(record.msg, str):
                record.msg = self._mask(record.msg)
            if isinstance(record.args, dict):
                record.args = {
                    key: self._mask(val) if isinstance(val, str) else val
                    for key, val in record.args.items()
                }
            elif isinstance(record.args, tuple):
                record.args = tuple(
                    self._mask(arg) if isinstance(arg, str) else arg
                    for arg in record.args
                )
        return True


def _env_secrets() -> list[str]:
    """Best-effort secret list from the environment (independent of Settings)."""
    return [
        value for value in (os.getenv("BOT_TOKEN"), os.getenv("VPN_TOKEN")) if value
    ]


def configure_logging(
    level: str = "INFO",
    secrets: Iterable[str] | None = None,
) -> None:
    """Configure root logging to stdout, masking secrets in every record."""
    resolved = list(secrets) if secrets is not None else []
    for secret in _env_secrets():
        if secret not in resolved:
            resolved.append(secret)

    handler = logging.StreamHandler(sys.stdout)
    handler.setFormatter(logging.Formatter(_LOG_FORMAT, datefmt=_DATE_FORMAT))
    handler.addFilter(SecretMaskingFilter(resolved))

    root = logging.getLogger()
    root.handlers.clear()
    root.addHandler(handler)
    root.setLevel(level.upper())

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
import traceback
from collections.abc import Iterable

MASK = "***"
#: Secrets shorter than this are ignored to avoid over-masking ordinary text.
MIN_SECRET_LENGTH = 8

_LOG_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"
_DATE_FORMAT = "%Y-%m-%d %H:%M:%S"


def normalize_secrets(secrets: Iterable[str]) -> tuple[str, ...]:
    """Drop falsy/short values so ordinary text is never over-masked."""
    return tuple(s for s in secrets if s and len(s) >= MIN_SECRET_LENGTH)


def mask_secrets(value: str, secrets: Iterable[str]) -> str:
    """Replace every known secret in ``value`` with :data:`MASK`.

    Shared by the logging filter (stdout) and the M0-08 error reporter (owner
    alerts), so there is exactly one masking rule in the app.
    """
    for secret in secrets:
        if secret in value:
            value = value.replace(secret, MASK)
    return value


class SecretMaskingFilter(logging.Filter):
    """Replace secret substrings in log messages and arguments."""

    def __init__(self, secrets: Iterable[str] = ()) -> None:
        super().__init__()
        self._secrets = normalize_secrets(secrets)

    def _mask(self, value: str) -> str:
        return mask_secrets(value, self._secrets)

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
            # Tracebacks are rendered by the *formatter*, i.e. after filters have
            # run, so a secret inside an exception message would bypass the mask
            # above and reach stdout (M0-08.3 logs tracebacks). Render them here
            # instead; ``Formatter.format`` reuses ``record.exc_text``.
            if record.exc_info is not None:
                record.exc_text = self._mask(
                    "".join(traceback.format_exception(*record.exc_info))
                )
            elif record.exc_text:
                record.exc_text = self._mask(record.exc_text)
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

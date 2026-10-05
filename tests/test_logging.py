"""Tests for :mod:`app.logging_setup`."""

from __future__ import annotations

import logging
import sys

from app.logging_setup import (
    MASK,
    SecretMaskingFilter,
    configure_logging,
    mask_secrets,
    normalize_secrets,
)

_SECRET = "supersecrettoken123"


def _record(msg: object, args: object = None) -> logging.LogRecord:
    return logging.LogRecord("test", logging.INFO, "test.py", 1, msg, args, None)


def test_filter_masks_message() -> None:
    record = _record(f"token={_SECRET}")
    assert SecretMaskingFilter([_SECRET]).filter(record) is True
    assert record.getMessage() == f"token={MASK}"


def test_filter_masks_positional_args() -> None:
    record = _record("token=%s", (_SECRET,))
    SecretMaskingFilter([_SECRET]).filter(record)
    assert record.getMessage() == f"token={MASK}"


def test_filter_leaves_unrelated_text_untouched() -> None:
    record = _record("nothing sensitive here")
    SecretMaskingFilter([_SECRET]).filter(record)
    assert record.getMessage() == "nothing sensitive here"


def test_short_secrets_are_ignored() -> None:
    # Values shorter than the safety threshold must not over-mask normal text.
    record = _record("ab normal text ab")
    SecretMaskingFilter(["ab"]).filter(record)
    assert record.getMessage() == "ab normal text ab"


def test_filter_masks_traceback_text() -> None:
    """M0-08.3 logs tracebacks, which are rendered *after* filters run."""
    try:
        raise ValueError(f"bad token {_SECRET}")
    except ValueError:
        exc_info = sys.exc_info()

    record = _record("boom", None)
    record.exc_info = exc_info
    SecretMaskingFilter([_SECRET]).filter(record)

    assert record.exc_text is not None
    assert _SECRET not in record.exc_text
    assert MASK in record.exc_text

    formatter = logging.Formatter("%(message)s")
    assert _SECRET not in formatter.format(record)


def test_mask_secrets_helper() -> None:
    assert mask_secrets(f"a {_SECRET} b", [_SECRET]) == f"a {MASK} b"
    assert mask_secrets("nothing", [_SECRET]) == "nothing"
    # Short/falsy values are dropped by ``normalize_secrets``.
    assert normalize_secrets(["", "ab", _SECRET]) == (_SECRET,)


def test_configure_logging_masks_stdout(
    capsys,
    monkeypatch,
) -> None:
    monkeypatch.delenv("BOT_TOKEN", raising=False)
    monkeypatch.delenv("VPN_TOKEN", raising=False)
    configure_logging("INFO", secrets=[_SECRET])

    logging.getLogger("test").info("leaking %s now", _SECRET)

    out = capsys.readouterr().out
    assert _SECRET not in out
    assert MASK in out

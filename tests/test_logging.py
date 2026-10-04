"""Tests for :mod:`app.logging_setup`."""

from __future__ import annotations

import logging

from app.logging_setup import MASK, SecretMaskingFilter, configure_logging

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

"""Stage-1 UI helpers (``TASK_PLAN.md`` §S1-1.1–.5).

The ``bar`` / ``fmt_bytes`` boundaries required by the task AC live here (and
nowhere else) so the handler suite does not duplicate them (§S1-1.13).
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any

import pytest

from app.ui import bar, delete_if_photo, edit_or_send, fmt_bytes, fmt_left
from tests.fakes import FakeBot

NOW = 1_700_000_000_000
HOUR_MS = 3_600_000
DAY_MS = 24 * HOUR_MS


# --- bar (S1-1.1) ----------------------------------------------------------


@pytest.mark.parametrize(
    ("fraction", "expected"),
    [
        (0, "▱▱▱▱▱▱▱▱▱▱"),
        (1, "▰▰▰▰▰▰▰▰▰▰"),
        (2, "▰▰▰▰▰▰▰▰▰▰"),  # clamped to 1
        (0.5, "▰▰▰▰▰▱▱▱▱▱"),
        (-1, "▱▱▱▱▱▱▱▱▱▱"),  # clamped to 0
        (float("inf"), "▱▱▱▱▱▱▱▱▱▱"),  # non-finite → 0
        (float("nan"), "▱▱▱▱▱▱▱▱▱▱"),  # non-finite → 0
    ],
)
def test_bar_boundaries(fraction: float, expected: str) -> None:
    assert bar(fraction) == expected


def test_bar_uses_the_requested_width() -> None:
    assert bar(0.25, 4) == "▰▱▱▱"
    assert bar(0.5, 0) == ""


# --- fmt_bytes (S1-1.2) ----------------------------------------------------


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 Б"),
        (1023, "1023 Б"),
        (1024, "1.0 КБ"),
        (1536, "1.5 КБ"),
        (1024**2, "1.0 МБ"),
        (1024**3, "1.0 ГБ"),
        (1024**4, "1.0 ТБ"),
        (1024**5, "1024.0 ТБ"),  # capped at the largest unit
        (None, "0 Б"),
        (-1, "0 Б"),
    ],
)
def test_fmt_bytes_boundaries(value: int | None, expected: str) -> None:
    assert fmt_bytes(value) == expected


# --- fmt_left (S1-1.3) -----------------------------------------------------


@pytest.mark.parametrize(
    ("expiry_ms", "expected"),
    [
        (NOW + 37 * DAY_MS, "ещё 37 дн."),
        (NOW + DAY_MS, "ещё 1 дн."),
        (NOW + DAY_MS - 1, "ещё 23 ч."),
        (NOW + 5 * HOUR_MS, "ещё 5 ч."),
        (NOW + 60_000, "ещё 1 ч."),  # under an hour still shows one hour
        (NOW, "истёк"),
        (NOW - 1, "истёк"),
        (0, ""),
        (None, ""),
    ],
)
def test_fmt_left_branches(expiry_ms: int | None, expected: str) -> None:
    assert fmt_left(expiry_ms, NOW) == expected


# --- edit_or_send (S1-1.4) -------------------------------------------------


async def test_edit_or_send_edits_first(fake_bot: FakeBot) -> None:
    assert await edit_or_send(fake_bot, 99, 500, "hello") is True

    (chat_id, message_id, text, kwargs) = fake_bot.edits[0]
    assert (chat_id, message_id, text) == (99, 500, "hello")
    assert kwargs["parse_mode"] == "HTML"
    assert fake_bot.sent == []


async def test_edit_or_send_treats_not_modified_as_success(fake_bot: FakeBot) -> None:
    """AC: unchanged content must not produce a second message."""
    await edit_or_send(fake_bot, 99, 500, "same")
    await edit_or_send(fake_bot, 99, 500, "same")

    assert len(fake_bot.edits) == 1
    assert fake_bot.sent == []


async def test_edit_or_send_falls_back_to_send(fake_bot: FakeBot) -> None:
    """AC: a failed edit is answered with a fresh ``send_message``."""
    fake_bot.fail_edit = True

    assert await edit_or_send(fake_bot, 99, 500, "text", markup={"a": 1}) is True

    assert fake_bot.edits == []
    assert fake_bot.texts_to(99) == ["text"]


async def test_edit_or_send_sends_when_there_is_no_message(fake_bot: FakeBot) -> None:
    assert await edit_or_send(fake_bot, 99, None, "fresh") is True

    assert fake_bot.texts_to(99) == ["fresh"]


# --- delete_if_photo (S1-1.5) ----------------------------------------------


def _call(photo: Any = None) -> SimpleNamespace:
    """A callback query whose message may carry a photo."""
    return SimpleNamespace(
        message=SimpleNamespace(
            chat=SimpleNamespace(id=99), message_id=500, photo=photo
        )
    )


async def test_delete_if_photo_removes_the_message(fake_bot: FakeBot) -> None:
    await delete_if_photo(fake_bot, _call(photo=[{"file_id": "x"}]))

    assert fake_bot.deleted == [(99, 500)]


async def test_delete_if_photo_leaves_a_text_message_alone(fake_bot: FakeBot) -> None:
    await delete_if_photo(fake_bot, _call())

    assert fake_bot.deleted == []

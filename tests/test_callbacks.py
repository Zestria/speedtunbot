"""Typed callback + confirmation-store tests (``TASK_PLAN.md`` §2.6 / M0-07.10).

Covers the 64-byte guard, per-namespace round-trips, malformed/edited data
(``InvalidCallback``, fixes B5) and the :class:`Confirmations` TTL/ownership.
"""

from __future__ import annotations

import secrets

import pytest

from app import texts
from app.callbacks import (
    MAX_BYTES,
    TOKEN_URLSAFE_BYTES,
    Access,
    AdminGrant,
    AdminNav,
    Broadcast,
    Callback,
    Confirm,
    Confirmation,
    Confirmations,
    ConfirmResult,
    Pay,
    ProfileNav,
    Support,
    UserAction,
    pack,
    unpack,
)
from app.errors import InvalidCallback

# --- 64-byte guard (M0-07.5) -----------------------------------------------


def test_pack_rejects_payload_over_64_bytes() -> None:
    with pytest.raises(ValueError):
        pack("ns", "x" * 70)


def test_pack_accepts_exactly_64_bytes() -> None:
    data = pack("n", "a" * 62)
    assert len(data.encode("utf-8")) == 64


def test_pack_rejects_multibyte_overflow() -> None:
    # 40 Cyrillic chars = 80 UTF-8 bytes, even though the str is shorter.
    with pytest.raises(ValueError):
        pack("ns", "я" * 40)


# --- per-namespace round-trips (M0-07.6) -----------------------------------


@pytest.mark.parametrize(
    ("payload", "data"),
    [
        (Pay(action="sel", ref_id=7), "pay:sel:7"),
        (Access(action="ok", ref_id=12), "acc:ok:12"),
        (AdminGrant(action="no", ref_id=3), "adg:no:3"),
        (UserAction(action="ban", ref_id=99), "usr:ban:99"),
        (Support(action="reply", ref_id=5), "sup:reply:5"),
        (Broadcast(action="confirm", ref_id=1), "bc:confirm:1"),
        (AdminNav(section="users"), "adm:users"),
        (ProfileNav(section="vpn"), "prf:vpn"),
    ],
)
def test_namespace_round_trip(payload: Callback, data: str) -> None:
    assert payload.pack() == data
    assert unpack(data) == payload


#: Stage-1 profile tokens (S1-1.7) — declared before their screens exist.
PROFILE_SECTIONS = (
    "qr",
    "instr",
    "instr_android",
    "instr_ios",
    "instr_windows",
    "instr_macos",
    "link",
    "newlink",
)


@pytest.mark.parametrize("section", PROFILE_SECTIONS)
def test_profile_nav_new_sections_round_trip(section: str) -> None:
    """AC (S1-1.7): every token round-trips and fits the 64-byte budget."""
    payload = ProfileNav(section=section)
    data = payload.pack()

    assert data == f"prf:{section}"
    assert unpack(data) == payload
    assert len(data.encode("utf-8")) <= MAX_BYTES


def test_profile_nav_rejects_an_unknown_section() -> None:
    with pytest.raises(InvalidCallback):
        unpack("prf:nope")


@pytest.mark.parametrize("platform", texts.INSTRUCTION_PLATFORMS)
def test_every_instruction_platform_has_a_profile_section(platform: str) -> None:
    """AC (S1-3.2): each picker button is a valid ``ProfileNav`` section.

    The instruction catalogue (``app.texts``) and the callback namespace are
    edited by different tasks, so they are allowed to drift apart only loudly.
    """
    payload = ProfileNav(section=f"instr_{platform}")

    assert payload.pack() == f"prf:instr_{platform}"
    assert unpack(payload.pack()) == payload


def test_confirm_namespace_round_trip() -> None:
    assert Confirm(token="tok").pack() == "cf:tok"
    assert Confirm(token="tok", cancel=True).pack() == "cf:x:tok"
    assert unpack("cf:tok") == Confirm(token="tok", cancel=False)
    assert unpack("cf:x:tok") == Confirm(token="tok", cancel=True)


def test_confirm_token_payload_stays_within_budget() -> None:
    token = secrets.token_urlsafe(TOKEN_URLSAFE_BYTES)
    assert len(token) == 11  # token_urlsafe(8) → 11 chars
    for payload in (Confirm(token=token), Confirm(token=token, cancel=True)):
        assert len(payload.pack().encode("utf-8")) <= MAX_BYTES


# --- malformed / edited data raises InvalidCallback (fixes B5) -------------


@pytest.mark.parametrize(
    "data",
    [
        "",
        "bogus",  # unknown namespace
        "bogus:a:b",
        "pay",  # too few segments
        "pay:sel",  # missing id
        "pay:sel:1:2",  # too many segments
        "pay:sel:abc",  # id is not an int
        "pay:nope:1",  # unknown action
        "adm",  # menu: missing section
        "adm:users:1",  # menu: too many segments
        "adm:nope",  # unknown section
        "cf:",  # empty token
        "cf:x:",  # empty cancel token
        "cf:y:tok",  # malformed cancel marker
    ],
)
def test_unpack_rejects_malformed_data(data: str) -> None:
    with pytest.raises(InvalidCallback):
        unpack(data)


def test_unpack_rejects_none() -> None:
    with pytest.raises(InvalidCallback):
        unpack(None)


# --- Confirmations store (M0-07.7) -----------------------------------------


class _Clock:
    """Manually advanced monotonic clock for TTL tests."""

    def __init__(self, start: float = 1000.0) -> None:
        self.now = start

    def __call__(self) -> float:
        return self.now


def test_confirmations_create_and_consume() -> None:
    store = Confirmations(ttl=60.0, clock=_Clock())
    token = store.create(1, "pay.approve", 5)
    result = store.consume(token, admin_id=1, action="pay.approve")
    assert result == Confirmation(ConfirmResult.OK, (5,))
    assert result.ok
    # Consumed tokens cannot be replayed.
    assert store.consume(token, 1, "pay.approve").status is ConfirmResult.EXPIRED


def test_confirmations_bound_to_admin_and_action() -> None:
    store = Confirmations(clock=_Clock())
    token = store.create(1, "pay.approve", 5)
    assert store.consume(token, 2, "pay.approve").status is ConfirmResult.FORBIDDEN
    assert store.consume(token, 1, "pay.decline").status is ConfirmResult.FORBIDDEN
    # A failed consume must not burn the token.
    assert store.consume(token, 1, "pay.approve").status is ConfirmResult.OK


def test_confirmations_expire_after_ttl() -> None:
    clock = _Clock()
    store = Confirmations(ttl=60.0, clock=clock)
    token = store.create(1, "users.delete", 42)
    clock.now += 61.0
    assert store.consume(token, 1, "users.delete").status is ConfirmResult.EXPIRED


def test_confirmations_cancel_and_purge() -> None:
    clock = _Clock()
    store = Confirmations(ttl=60.0, clock=clock)
    first = store.create(1, "a")
    store.create(1, "b")
    assert store.cancel(first, admin_id=1) is True
    assert store.consume(first, 1, "a").status is ConfirmResult.EXPIRED
    # Cancelling someone else's token is refused.
    other = store.create(2, "c")
    assert store.cancel(other, admin_id=1) is False
    clock.now += 61.0
    assert store.purge() == 2
    assert len(store) == 0

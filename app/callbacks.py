"""Typed callback payloads (``TASK_PLAN.md`` §2.6 / M0-07.5–7).

Format: ``<ns>:<action>[:<arg>...]`` where args are ints or short tokens.

Every payload is a frozen dataclass with :meth:`pack` / :meth:`parse`.
:func:`unpack` validates the segment count and types and raises
:class:`~app.errors.InvalidCallback` for anything malformed, short or edited —
handlers answer "Кнопка устарела" instead of crashing (fixes defect B5).

``pack`` refuses payloads longer than :data:`MAX_BYTES` with an explicit
:class:`ValueError` (not ``assert``: asserts vanish under ``python -O``).

Byte budget: the ``cf`` payload is ``cf:<token>`` (or ``cf:x:<token>``), so a
:meth:`Confirmations` token of ``secrets.token_urlsafe(8)`` (11 chars) leaves
~50 bytes for the prefix/action — comfortable, but when adding new token-based
namespaces keep the 64-**byte** (not character) limit in mind; see
:data:`TOKEN_URLSAFE_BYTES`.

Never encode the *clicking* user's id/chat in callback data — derive it from
``call``. Target ids (the user being acted upon) are fine.
"""

from __future__ import annotations

import secrets
import time
from collections.abc import Callable
from dataclasses import dataclass
from enum import StrEnum
from typing import ClassVar

from app.errors import InvalidCallback

MAX_BYTES = 64
#: Number of random bytes behind a :class:`Confirmations` token. ``token_urlsafe``
#: expands this to 11 chars (``ceil(n/3)*4``), which is the dominant contributor
#: to the ``cf:`` callback-data size — keep it well under :data:`MAX_BYTES`.
TOKEN_URLSAFE_BYTES = 8


def pack(*segments: object) -> str:
    """Join ``segments`` with ``:`` and reject payloads longer than 64 bytes.

    Raises:
        ValueError: when the encoded payload exceeds :data:`MAX_BYTES`.
    """
    data = ":".join(str(segment) for segment in segments)
    if len(data.encode("utf-8")) > MAX_BYTES:
        raise ValueError(
            f"callback data is {len(data.encode('utf-8'))} bytes "
            f"(max {MAX_BYTES}): {data!r}"
        )
    return data


@dataclass(frozen=True)
class Callback:
    """Base class for every typed callback payload."""

    #: Registry namespace (see the module docstring).
    ns: ClassVar[str] = ""

    def parts(self) -> tuple[str, ...]:
        """Return the raw segments for this payload (ns first)."""
        raise NotImplementedError

    def pack(self) -> str:
        """Serialise to callback data, enforcing the 64-byte limit."""
        return pack(*self.parts())

    @classmethod
    def parse(cls, segments: list[str]) -> Callback:
        """Rebuild the payload from ``segments``; raise :class:`InvalidCallback`."""
        raise NotImplementedError


#: ``<ns>`` → parser. Populated at import time by :func:`_register`.
_REGISTRY: dict[str, Callable[[list[str]], Callback]] = {}


def _register(cls: type[Callback]) -> type[Callback]:
    """Register a payload class under its ``ns``."""
    _REGISTRY[cls.ns] = cls.parse
    return cls


def unpack(data: str | None) -> Callback:
    """Parse callback ``data`` into a typed payload.

    Raises:
        InvalidCallback: when ``data`` is empty, has an unknown namespace, or
            fails the namespace's own shape/type validation.
    """
    if not data:
        raise InvalidCallback("empty callback data")
    segments = data.split(":")
    parser = _REGISTRY.get(segments[0])
    if parser is None:
        raise InvalidCallback(f"unknown callback namespace: {segments[0]!r}")
    try:
        return parser(segments)
    except InvalidCallback:
        raise
    except (ValueError, TypeError, IndexError) as exc:
        raise InvalidCallback(f"malformed callback data: {data!r}") from exc


def _to_int(raw: str) -> int:
    """Parse ``raw`` as an int or raise :class:`InvalidCallback`."""
    try:
        return int(raw)
    except ValueError as exc:
        raise InvalidCallback(f"expected an integer argument, got {raw!r}") from exc


@dataclass(frozen=True)
class _IntAction(Callback):
    """Shared ``<ns>:<action>:<int>`` shape (ids/actions, no clicker identity)."""

    ACTIONS: ClassVar[frozenset[str]] = frozenset()

    action: str
    ref_id: int

    def parts(self) -> tuple[str, ...]:
        return (self.ns, self.action, str(self.ref_id))

    @classmethod
    def parse(cls, segments: list[str]) -> _IntAction:
        if len(segments) != 3:
            raise InvalidCallback(f"{cls.ns}: expected 3 segments, got {segments!r}")
        ns, action, raw = segments
        if ns != cls.ns:
            raise InvalidCallback(f"{cls.ns}: wrong namespace {ns!r}")
        if action not in cls.ACTIONS:
            raise InvalidCallback(f"{cls.ns}: unknown action {action!r}")
        return cls(action=action, ref_id=_to_int(raw))


@dataclass(frozen=True)
class _Menu(Callback):
    """Shared ``<ns>:<section>`` shape for keyboard navigation."""

    SECTIONS: ClassVar[frozenset[str]] = frozenset()

    section: str

    def parts(self) -> tuple[str, ...]:
        return (self.ns, self.section)

    @classmethod
    def parse(cls, segments: list[str]) -> _Menu:
        if len(segments) != 2:
            raise InvalidCallback(f"{cls.ns}: expected 2 segments, got {segments!r}")
        ns, section = segments
        if ns != cls.ns:
            raise InvalidCallback(f"{cls.ns}: wrong namespace {ns!r}")
        if section not in cls.SECTIONS:
            raise InvalidCallback(f"{cls.ns}: unknown section {section!r}")
        return cls(section=section)


@_register
@dataclass(frozen=True)
class Pay(_IntAction):
    """Payments: ``pay:<action>:<id>`` (M0-10)."""

    ns: ClassVar[str] = "pay"
    #: ``sel`` ships a tariff id, the rest a payment id — both just an int.
    ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"sel", "done", "cancel", "ok", "no", "retry"}
    )


@_register
@dataclass(frozen=True)
class Access(_IntAction):
    """Access requests: ``acc:<ok|no>:<request_id>`` (M1)."""

    ns: ClassVar[str] = "acc"
    ACTIONS: ClassVar[frozenset[str]] = frozenset({"ok", "no"})


@_register
@dataclass(frozen=True)
class AdminGrant(_IntAction):
    """Admin-grant requests: ``adg:<ok|no>:<request_id>``."""

    ns: ClassVar[str] = "adg"
    ACTIONS: ClassVar[frozenset[str]] = frozenset({"ok", "no"})


@_register
@dataclass(frozen=True)
class UserAction(_IntAction):
    """User card actions: ``usr:<action>:<tg_id>``."""

    ns: ClassVar[str] = "usr"
    ACTIONS: ClassVar[frozenset[str]] = frozenset(
        {"card", "ban", "unban", "grant", "expiry", "traffic", "reset_sub", "delete"}
    )


@_register
@dataclass(frozen=True)
class Support(_IntAction):
    """Support flow: ``sup:<action>:<tg_id>``."""

    ns: ClassVar[str] = "sup"
    ACTIONS: ClassVar[frozenset[str]] = frozenset({"reply", "close"})


@_register
@dataclass(frozen=True)
class Broadcast(_IntAction):
    """Broadcast jobs: ``bc:<action>:<job_id>`` (M2-06)."""

    ns: ClassVar[str] = "bc"
    ACTIONS: ClassVar[frozenset[str]] = frozenset({"confirm", "cancel", "retry"})


@_register
@dataclass(frozen=True)
class AdminNav(Callback):
    """Admin panel navigation: ``adm:<section>[:<action>[:<arg>]]`` (§S2-1.1).

    ``section`` names the *screen* (validated against :data:`SECTIONS`); the
    optional ``action``/``arg`` carry a screen-local command (e.g.
    ``adm:users:card:123``). The fixed 2–4-segment shape means a new screen or
    action never needs a new telebot callback handler — the same reason Stage 1
    used one ``prf:`` handler (§S2-1).
    """

    ns: ClassVar[str] = "adm"
    SECTIONS: ClassVar[frozenset[str]] = frozenset(
        {
            "menu",
            "users",
            "payments",
            "access",
            "invites",
            "admins",
            "settings",
            "server",
            "audit",
            "broadcast",
            "back",
        }
    )

    section: str
    action: str | None = None
    arg: str | None = None

    def parts(self) -> tuple[str, ...]:
        parts: list[str] = [self.ns, self.section]
        if self.action is not None:
            parts.append(self.action)
            if self.arg is not None:
                parts.append(self.arg)
        return tuple(parts)

    @classmethod
    def parse(cls, segments: list[str]) -> AdminNav:
        if not 2 <= len(segments) <= 4:
            raise InvalidCallback(f"adm: expected 2-4 segments, got {segments!r}")
        ns, section = segments[0], segments[1]
        if ns != cls.ns:
            raise InvalidCallback(f"adm: wrong namespace {ns!r}")
        if section not in cls.SECTIONS:
            raise InvalidCallback(f"adm: unknown section {section!r}")
        action = segments[2] if len(segments) >= 3 else None
        arg = segments[3] if len(segments) == 4 else None
        return cls(section=section, action=action, arg=arg)


@_register
@dataclass(frozen=True)
class ProfileNav(_Menu):
    """Profile navigation: ``prf:<section>``."""

    ns: ClassVar[str] = "prf"
    SECTIONS: ClassVar[frozenset[str]] = frozenset(
        {
            "profile",
            "pay",
            "support",
            "vpn",
            "back",
            # Stage 1 screens (§S1-1.7): QR, instruction pickers, link screen
            # and regeneration. Declared up front so the namespace stays stable.
            "qr",
            "instr",
            "instr_android",
            "instr_ios",
            "instr_windows",
            "instr_macos",
            "link",
            "newlink",
        }
    )


@_register
@dataclass(frozen=True)
class Confirm(Callback):
    """Server-side confirmation button: ``cf:<token>`` / ``cf:x:<token>``.

    ``cf:x:<token>`` is the cancel button; the token itself never encodes the
    action, which lives only in the :class:`Confirmations` store (§2.6).
    """

    ns: ClassVar[str] = "cf"

    token: str
    cancel: bool = False

    def parts(self) -> tuple[str, ...]:
        if self.cancel:
            return (self.ns, "x", self.token)
        return (self.ns, self.token)

    @classmethod
    def parse(cls, segments: list[str]) -> Confirm:
        if len(segments) == 2:
            ns, token = segments
            return cls(token=_valid_token(ns, token), cancel=False)
        if len(segments) == 3 and segments[1] == "x":
            ns, _, token = segments
            return cls(token=_valid_token(ns, token), cancel=True)
        raise InvalidCallback(f"cf: malformed segments {segments!r}")


def _valid_token(ns: str, token: str) -> str:
    """Validate the ``cf`` namespace and that ``token`` is non-empty."""
    if ns != Confirm.ns:
        raise InvalidCallback(f"cf: wrong namespace {ns!r}")
    if not token:
        raise InvalidCallback("cf: empty token")
    return token


class ConfirmResult(StrEnum):
    """Outcome of :meth:`Confirmations.consume`."""

    OK = "ok"
    EXPIRED = "expired"
    INVALID = "invalid"
    FORBIDDEN = "forbidden"


@dataclass(frozen=True)
class Confirmation:
    """Result of consuming a confirmation token."""

    status: ConfirmResult
    args: tuple[object, ...] = ()

    @property
    def ok(self) -> bool:
        return self.status is ConfirmResult.OK


@dataclass(frozen=True)
class _Pending:
    """A live confirmation token, bound to admin + action + args."""

    admin_id: int
    action: str
    args: tuple[object, ...]
    expires_at: float


DEFAULT_CONFIRM_TTL = 60.0


class Confirmations:
    """In-memory, TTL-bounded store backing ``cf:`` buttons (§2.6).

    Tokens are bound to the acting admin, the action and its args, so a
    confirmation minted for one admin/action can never be reused for another.
    ``clock`` (monotonic seconds) and ``token_factory`` are injectable for tests.
    """

    def __init__(
        self,
        ttl: float = DEFAULT_CONFIRM_TTL,
        *,
        clock: Callable[[], float] = time.monotonic,
        token_factory: Callable[[], str] | None = None,
    ) -> None:
        self._ttl = ttl
        self._clock = clock
        self._token_factory = token_factory or self._random_token
        self._pending: dict[str, _Pending] = {}

    @staticmethod
    def _random_token() -> str:
        return secrets.token_urlsafe(TOKEN_URLSAFE_BYTES)

    def create(self, admin_id: int, action: str, *args: object) -> str:
        """Mint a fresh token for ``admin_id``/``action``/``args`` and return it."""
        self.purge()
        token = self._token_factory()
        self._pending[token] = _Pending(
            admin_id=int(admin_id),
            action=action,
            args=tuple(args),
            expires_at=self._clock() + self._ttl,
        )
        return token

    def consume(self, token: str, admin_id: int, action: str) -> Confirmation:
        """Validate and consume ``token``; expired tokens are purged first."""
        self.purge()
        pending = self._pending.get(token)
        if pending is None:
            return Confirmation(ConfirmResult.EXPIRED)
        if pending.admin_id != int(admin_id) or pending.action != action:
            return Confirmation(ConfirmResult.FORBIDDEN)
        del self._pending[token]
        return Confirmation(ConfirmResult.OK, pending.args)

    def peek(self, token: str, admin_id: int) -> str | None:
        """Return the action bound to a live ``token`` owned by ``admin_id``.

        A non-destructive lookup (the token itself never encodes the action, so
        the central ``cf:`` dispatcher must learn it *before* :meth:`consume`).
        ``None`` when the token is unknown, expired, or belongs to somebody else;
        an unknown owner therefore never burns another user's token.
        """
        self.purge()
        pending = self._pending.get(token)
        if pending is None or pending.admin_id != int(admin_id):
            return None
        return pending.action

    def cancel(self, token: str, admin_id: int) -> bool:
        """Drop ``token`` when it belongs to ``admin_id`` (``cf:x:`` button)."""
        pending = self._pending.get(token)
        if pending is None or pending.admin_id != int(admin_id):
            return False
        del self._pending[token]
        return True

    def purge(self) -> int:
        """Drop every expired token; return how many were removed."""
        now = self._clock()
        stale = [tok for tok, p in self._pending.items() if p.expires_at <= now]
        for tok in stale:
            del self._pending[tok]
        return len(stale)

    def __len__(self) -> int:
        return len(self._pending)

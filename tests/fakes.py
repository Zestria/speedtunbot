"""In-memory stand-in for :class:`app.services.panel.PanelGateway`.

``FakePanel`` mirrors the gateway's public surface so services and handlers can
be unit-tested without a live 3x-ui panel. Writes are serialised with an
:class:`asyncio.Lock` just like the real gateway, and a handful of failure
switches (``unavailable``, ``drop_writes``, ``latency``) allow the negative
paths from ``TASK_PLAN.md`` §M0-04 to be exercised.
"""

from __future__ import annotations

import asyncio
import time
import uuid
from collections.abc import AsyncIterator, Callable
from contextlib import asynccontextmanager
from dataclasses import dataclass
from typing import Any

from py3xui import Client

from app.errors import ClientNotFound, NotSupportedError, PanelError, PanelUnavailable
from app.services.panel import ClientTraffic, new_sub_id


@dataclass
class _ClientStats:
    """Per-client traffic counters, kept apart from the settings copy."""

    up: int = 0
    down: int = 0
    last_online_ms: int | None = None


class FakePanel:
    """A tiny in-memory panel.

    Failure switches:

    * ``unavailable`` — every call raises :class:`PanelUnavailable`;
    * ``drop_writes`` — updates are silently discarded, so the post-write
      verification raises :class:`PanelError`;
    * ``latency`` — added to every call (used to test timeouts/ordering).

    Per-client traffic is kept in ``_stats``, separate from ``_clients`` (the
    settings copy), mirroring the real panel: ``get_traffic`` merges the two
    and ``reset_traffic`` zeroes only the stats (S0-2.6).
    """

    def __init__(self) -> None:
        self._clients: dict[int, Client] = {}
        self._stats: dict[int, _ClientStats] = {}
        self._lock = asyncio.Lock()
        self.unavailable = False
        self.drop_writes = False
        self.latency = 0.0
        self.calls: list[str] = []
        #: Toggled by ``restart_xray`` so the server screen shows the effect (§S2-5.2).
        self.xray_running = True

    # --- test helpers ------------------------------------------------------

    def seed(
        self,
        tg_id: int,
        *,
        expiry_ms: int | None = None,
        enable: bool = True,
        sub_id: str | None = None,
        total_gb: int = 0,
        up: int = 0,
        down: int = 0,
        last_online_ms: int | None = None,
    ) -> Client:
        """Insert a client directly (bypassing the gateway)."""
        client = Client(
            id=str(uuid.uuid4()),
            email=str(tg_id),
            enable=enable,
            expiry_time=int(expiry_ms or 0),
            sub_id=sub_id if sub_id is not None else new_sub_id(),
            total_gb=total_gb,
        )
        self._clients[tg_id] = client
        self._stats[tg_id] = _ClientStats(
            up=int(up), down=int(down), last_online_ms=last_online_ms
        )
        return client

    def _guard(self, name: str) -> None:
        self.calls.append(name)
        if self.unavailable:
            raise PanelUnavailable(f"fake panel unavailable ({name})")
        if self.latency:
            time.sleep(self.latency)

    def _get(self, tg_id: int) -> Client:
        client = self._clients.get(int(tg_id))
        if client is None:
            raise ClientNotFound(str(tg_id))
        return client

    # --- reads -------------------------------------------------------------

    async def get_client(self, tg_id: int) -> Client | None:
        self._guard("get_client")
        client = self._clients.get(int(tg_id))
        return None if client is None else client.model_copy(deep=True)

    async def list_clients(self) -> list[Client]:
        self._guard("list_clients")
        return [c.model_copy(deep=True) for c in self._clients.values()]

    async def get_traffic(self, tg_id: int) -> ClientTraffic | None:
        self._guard("get_traffic")
        client = self._clients.get(int(tg_id))
        if client is None:
            return None
        stats = self._stats.get(int(tg_id), _ClientStats())
        return ClientTraffic(
            email=client.email,
            up=stats.up,
            down=stats.down,
            total=int(client.total),
            expiry_ms=int(client.expiry_time),
            enable=bool(client.enable),
            sub_id=client.sub_id,
            last_online_ms=stats.last_online_ms,
        )

    async def server_status(self) -> dict[str, Any]:
        self._guard("server_status")
        return {
            "cpu": 18.0,
            "mem": {"current": 52, "total": 100},
            "disk": {"current": 31, "total": 100},
            "uptime": 1_058_400,
            "xray": {
                "state": "running" if self.xray_running else "stopped",
                "version": "1.8",
            },
            "online": len(self._clients),
        }

    # --- writes ------------------------------------------------------------

    async def mutate(
        self,
        tg_id: int,
        apply: Callable[[Client], bool],
        verify: Callable[[Client], bool],
        *,
        desc: str = "change",
    ) -> Client:
        self._guard("mutate")
        async with self._lock:
            work = self._get(tg_id).model_copy(deep=True)
            # Yield inside the lock so a missing lock would be observable in
            # concurrent read-modify-write tests.
            await asyncio.sleep(0)
            changed = apply(work)
            if changed and not self.drop_writes:
                self._clients[int(tg_id)] = work
            fresh = self._clients[int(tg_id)]
        if changed and not verify(fresh):
            raise PanelError(f"panel did not persist {desc} for client {tg_id}")
        return fresh.model_copy(deep=True)

    async def set_enabled(self, tg_id: int, enabled: bool) -> Client:
        enabled = bool(enabled)

        def apply(client: Client) -> bool:
            client.enable = enabled
            return True

        return await self.mutate(
            tg_id, apply, lambda c: bool(c.enable) == enabled, desc="enable"
        )

    async def set_expiry_ms(self, tg_id: int, ms: int) -> Client:
        ms = int(ms)

        def apply(client: Client) -> bool:
            client.expiry_time = ms
            return True

        return await self.mutate(
            tg_id, apply, lambda c: int(c.expiry_time) == ms, desc="expiry"
        )

    async def set_limits(
        self, tg_id: int, total_bytes: int | None = None, limit_ip: int | None = None
    ) -> Client:
        total = 0 if total_bytes is None else max(0, int(total_bytes))
        ips = 0 if limit_ip is None else max(0, int(limit_ip))

        def apply(client: Client) -> bool:
            # The gateway writes ``total_gb`` but reads ``Client.total`` back in
            # ``get_traffic``, so the fake mirrors the quota into both fields —
            # otherwise a limit set through the card would read as «∞» (§S2-3.8).
            client.total_gb = total
            client.total = total
            client.limit_ip = ips
            return True

        return await self.mutate(
            tg_id,
            apply,
            lambda c: int(c.total_gb) == total and int(c.limit_ip) == ips,
            desc="limits",
        )

    async def reset_traffic(self, tg_id: int) -> Client:
        self._guard("reset_traffic")
        async with self._lock:
            client = self._get(tg_id)
            # Only the stats are zeroed; the settings copy stays untouched.
            self._stats[int(tg_id)] = _ClientStats()
        return client.model_copy(deep=True)

    async def ensure_client(self, tg_id: int, username: str = "") -> Client:
        self._guard("ensure_client")
        async with self._lock:
            existing = self._clients.get(int(tg_id))
            if existing is not None:
                return existing.model_copy(deep=True)
            client = Client(
                id=str(uuid.uuid4()),
                email=str(tg_id),
                enable=False,
                expiry_time=int(time.time() * 1000),
                sub_id=new_sub_id(),
                comment=f"@{username}" if username else "",
            )
            self._clients[int(tg_id)] = client
            return client.model_copy(deep=True)

    async def regenerate_sub_id(self, tg_id: int) -> str:
        sub_id = new_sub_id()

        def apply(client: Client) -> bool:
            client.sub_id = sub_id
            return True

        await self.mutate(tg_id, apply, lambda c: c.sub_id == sub_id, desc="sub_id")
        return sub_id

    async def delete_client(self, tg_id: int) -> None:
        self._guard("delete_client")
        self._get(tg_id)
        self._clients.pop(int(tg_id), None)

    async def restart_xray(self) -> None:
        """Record the call and toggle the fake Xray state (§S2-5.2)."""
        self._guard("restart_xray")
        self.xray_running = not self.xray_running

    async def download_panel_db(self, path: str = "") -> None:
        self._guard("download_panel_db")
        raise NotSupportedError("panel DB export is not supported by py3xui")


class FakeMessage:
    """Slim stand-in for a telebot ``Message`` returned by ``FakeBot``."""

    def __init__(self, message_id: int) -> None:
        self.message_id = int(message_id)


class FakeBot:
    """Records ``send_message`` / ``answer_callback_query`` calls (no I/O).

    Used by RBAC tests (M0-06) and reusable wherever a handler-level bot double
    is needed; it mirrors just the slice of the telebot surface the handlers and
    the permission helpers touch. Since M0-09 it also implements the tiny FSM
    slice the support relay needs (``set_state``/``get_state``/``delete_state``/
    ``retrieve_data``).
    """

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.callback_answers: list[tuple[str, str | None, bool]] = []
        #: Same messages as ``sent`` plus the keyword arguments, for HTML tests.
        self.messages: list[tuple[int, str, dict[str, Any]]] = []
        #: ``(chat_id, message_id, text, kwargs)`` for ``edit_message_text``.
        self.edits: list[tuple[int, int, str, dict[str, Any]]] = []
        #: ``(chat_id, message_id)`` for every ``delete_message`` call.
        self.deleted: list[tuple[int, int]] = []
        #: ``(chat_id, kind, file, kwargs)`` for ``send_photo``/``send_document``.
        #: ``file`` is the object the handler passed (a ``str`` file id **or** a
        #: ``BytesIO``), so tests can assert on in-memory uploads too.
        self.media: list[tuple[int, str, Any, dict[str, Any]]] = []
        #: ``(chat_id, [(command, description)])`` for every ``set_my_commands``
        #: call; ``chat_id`` is ``None`` for the default scope (§S4-1).
        self.command_menus: list[tuple[int | None, list[tuple[str, str]]]] = []
        #: Chats whose per-chat menu publish raises, like a chat the bot never
        #: talked to (``400 chat not found``) — §S4-1.
        self.fail_commands_for: set[int] = set()
        #: Raise ``RuntimeError`` on every edit (exercises the send fallback).
        self.fail_edit = False
        self._edited: dict[tuple[int, int], str] = {}
        self._states: dict[tuple[int, int], str | None] = {}
        self._data: dict[tuple[int, int], dict[str, Any]] = {}
        self._next_message_id = 1000

    async def send_message(
        self, chat_id: int, text: str, **kwargs: object
    ) -> FakeMessage:
        self.sent.append((int(chat_id), text))
        self.messages.append((int(chat_id), text, dict(kwargs)))
        self._next_message_id += 1
        return FakeMessage(self._next_message_id)

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
        **kwargs: object,
    ) -> None:
        self.callback_answers.append((callback_query_id, text, show_alert))

    # --- edits / deletes (Stage-1 screens) ---------------------------------

    async def edit_message_text(
        self, text: str, chat_id: int, message_id: int, **kwargs: object
    ) -> FakeMessage:
        """Edit a message; mirror Telegram's ``message is not modified`` error.

        Editing a message to the text it already holds is a no-op with a ``400``
        in the real API, so re-rendering the same card must not raise here
        either (S1-1.4).
        """
        if self.fail_edit:
            raise RuntimeError("Bad Request: message to edit not found")
        key = (int(chat_id), int(message_id))
        if self._edited.get(key) == text:
            raise RuntimeError("Bad Request: message is not modified")
        self._edited[key] = text
        self.edits.append((int(chat_id), int(message_id), text, dict(kwargs)))
        return FakeMessage(int(message_id))

    async def delete_message(self, chat_id: int, message_id: int) -> None:
        self.deleted.append((int(chat_id), int(message_id)))
        self._edited.pop((int(chat_id), int(message_id)), None)

    # --- media (receipts / support attachments) ----------------------------

    async def send_photo(
        self, chat_id: int, photo: Any, **kwargs: object
    ) -> FakeMessage:
        self.media.append((int(chat_id), "photo", photo, dict(kwargs)))
        self._next_message_id += 1
        return FakeMessage(self._next_message_id)

    async def send_document(
        self, chat_id: int, document: Any, **kwargs: object
    ) -> FakeMessage:
        self.media.append((int(chat_id), "document", document, dict(kwargs)))
        self._next_message_id += 1
        return FakeMessage(self._next_message_id)

    # --- FSM slice (support relay) -----------------------------------------

    async def set_state(
        self, user_id: int, state: object, chat_id: int | None = None
    ) -> None:
        name = getattr(state, "name", state)
        self._states[(int(user_id), int(chat_id or 0))] = name

    async def get_state(self, user_id: int, chat_id: int | None = None) -> str | None:
        return self._states.get((int(user_id), int(chat_id or 0)))

    async def delete_state(self, user_id: int, chat_id: int | None = None) -> None:
        self._states.pop((int(user_id), int(chat_id or 0)), None)

    @asynccontextmanager
    async def retrieve_data(
        self, user_id: int, chat_id: int | None = None
    ) -> AsyncIterator[dict[str, Any]]:
        yield self._data.setdefault((int(user_id), int(chat_id or 0)), {})

    # --- command menus (§S4-1) ---------------------------------------------

    async def set_my_commands(
        self, commands: Any, scope: Any = None, **kwargs: object
    ) -> None:
        """Record a published command menu, keyed by the scope's chat (or ``None``)."""
        chat_id = getattr(scope, "chat_id", None)
        if chat_id in self.fail_commands_for:
            raise RuntimeError("Bad Request: chat not found")
        self.command_menus.append(
            (chat_id, [(button.command, button.description) for button in commands])
        )

    # --- assertions helpers -------------------------------------------------

    def commands_to(self, chat_id: int | None) -> list[tuple[str, str]] | None:
        """Return the last menu published to ``chat_id`` (``None`` = default scope)."""
        for cid, menu in reversed(self.command_menus):
            if cid == chat_id:
                return menu
        return None

    def texts_to(self, chat_id: int) -> list[str]:
        """Return every text sent to ``chat_id`` (in order)."""
        return [text for cid, text, _ in self.messages if cid == int(chat_id)]

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
from collections.abc import Callable
from typing import Any

from py3xui import Client

from app.errors import ClientNotFound, NotSupportedError, PanelError, PanelUnavailable
from app.services.panel import ClientTraffic, new_sub_id


class FakePanel:
    """A tiny in-memory panel.

    Failure switches:

    * ``unavailable`` — every call raises :class:`PanelUnavailable`;
    * ``drop_writes`` — updates are silently discarded, so the post-write
      verification raises :class:`PanelError`;
    * ``latency`` — added to every call (used to test timeouts/ordering).
    """

    def __init__(self) -> None:
        self._clients: dict[int, Client] = {}
        self._lock = asyncio.Lock()
        self.unavailable = False
        self.drop_writes = False
        self.latency = 0.0
        self.calls: list[str] = []

    # --- test helpers ------------------------------------------------------

    def seed(
        self,
        tg_id: int,
        *,
        expiry_ms: int | None = None,
        enable: bool = True,
        sub_id: str | None = None,
        total_gb: int = 0,
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
        return ClientTraffic(
            email=client.email,
            up=int(client.up),
            down=int(client.down),
            total=int(client.total),
            expiry_ms=int(client.expiry_time),
            enable=bool(client.enable),
            sub_id=client.sub_id,
        )

    async def server_status(self) -> dict[str, Any]:
        self._guard("server_status")
        return {"cpu": 1.0, "mem": 2.0, "online": len(self._clients)}

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
        self, tg_id: int, total_gb: int | None = None, limit_ip: int | None = None
    ) -> Client:
        gb = 0 if total_gb is None else max(0, int(total_gb))
        ips = 0 if limit_ip is None else max(0, int(limit_ip))

        def apply(client: Client) -> bool:
            client.total_gb = gb
            client.limit_ip = ips
            return True

        return await self.mutate(
            tg_id,
            apply,
            lambda c: int(c.total_gb) == gb and int(c.limit_ip) == ips,
            desc="limits",
        )

    async def reset_traffic(self, tg_id: int) -> Client:
        def apply(client: Client) -> bool:
            client.up = 0
            client.down = 0
            return True

        return await self.mutate(
            tg_id, apply, lambda c: int(c.up) == 0 and int(c.down) == 0, desc="reset"
        )

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
        self._guard("restart_xray")
        raise NotSupportedError("Xray restart is not supported by py3xui")

    async def download_panel_db(self, path: str = "") -> None:
        self._guard("download_panel_db")
        raise NotSupportedError("panel DB export is not supported by py3xui")


class FakeBot:
    """Records ``send_message`` / ``answer_callback_query`` calls (no I/O).

    Used by RBAC tests (M0-06) and reusable wherever a handler-level bot double
    is needed; it mirrors just the slice of the telebot surface the handlers and
    the permission helpers touch.
    """

    def __init__(self) -> None:
        self.sent: list[tuple[int, str]] = []
        self.callback_answers: list[tuple[str, str | None, bool]] = []

    async def send_message(self, chat_id: int, text: str, **kwargs: object) -> None:
        self.sent.append((int(chat_id), text))

    async def answer_callback_query(
        self,
        callback_query_id: str,
        text: str | None = None,
        show_alert: bool = False,
        **kwargs: object,
    ) -> None:
        self.callback_answers.append((callback_query_id, text, show_alert))

"""3x-ui panel access (``TASK_PLAN.md`` §2.7 / task M0-04).

:class:`PanelGateway` is the **only** code that talks to 3x-ui. It wraps
``py3xui.AsyncApi`` in token-auth mode (never calls ``login()``), keeps a 30 s
``{email: Client}`` index built from ``inbound.settings.clients``, serialises
every write behind one global :class:`asyncio.Lock`, and re-reads the changed
fields after each write (bypassing the cache) to catch silent no-ops.

Implementation notes live in ``docs/panel_api_notes.md`` (M0-03); the decisions
behind the fallbacks are recorded in ``docs/DECISIONS.md``.
"""

from __future__ import annotations

import asyncio
import logging
import random
import string
import time
import uuid
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from typing import Any

import httpx
from py3xui import AsyncApi, Client

from app.errors import ClientNotFound, NotSupportedError, PanelError, PanelUnavailable
from app.settings import Settings

logger = logging.getLogger(__name__)

DEFAULT_TIMEOUT = 15.0
CACHE_TTL = 30.0
SUB_ID_LENGTH = 16
SUB_ID_ALPHABET = string.ascii_lowercase + string.digits

# Transport-level failures (never a domain error) mapped to PanelUnavailable.
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    OSError,
    asyncio.TimeoutError,
)


@dataclass(frozen=True)
class ClientTraffic:
    """Traffic snapshot for one panel client (read from the inbound copy)."""

    email: str
    up: int
    down: int
    total: int
    expiry_ms: int
    enable: bool
    sub_id: str

    @property
    def used(self) -> int:
        """Bytes consumed so far (``up`` + ``down``)."""
        return self.up + self.down


def new_sub_id() -> str:
    """Return a fresh 16-char ``[a-z0-9]`` subscription id (§2.7)."""
    return "".join(random.choices(SUB_ID_ALPHABET, k=SUB_ID_LENGTH))


class PanelGateway:
    """Single entry point for every 3x-ui interaction."""

    def __init__(
        self,
        settings: Settings,
        *,
        api: Any | None = None,
        timeout: float = DEFAULT_TIMEOUT,
    ) -> None:
        self._settings = settings
        self._inbound_id = settings.inbound_id
        self._timeout = timeout
        # Token auth only: ``AsyncApi(domain, token=...)``; ``login()`` is never
        # called (``loads.py:15``). ``api`` is injectable for tests.
        self._api = (
            api
            if api is not None
            else AsyncApi(settings.domain, token=settings.vpn_token)
        )
        # One global lock serialises every read-modify-write (lost-update race).
        self._lock = asyncio.Lock()
        self._clients: dict[str, Client] | None = None
        self._cache_at = 0.0

    # --- internals ---------------------------------------------------------

    async def _call(self, factory: Callable[[], Awaitable[Any]]) -> Any:
        """Await ``factory()`` with a timeout, mapping transport errors."""
        try:
            return await asyncio.wait_for(factory(), timeout=self._timeout)
        except PanelError:
            raise
        except _TRANSPORT_ERRORS as exc:
            raise PanelUnavailable(f"panel request failed: {exc}") from exc

    async def _read_clients(self) -> dict[str, Client]:
        """Fetch the inbound once and build an ``{email: Client}`` index."""
        inbound = await self._call(
            lambda: self._api.inbound.get_by_id(self._inbound_id)
        )
        settings = getattr(inbound, "settings", None)
        clients = getattr(settings, "clients", None) or []
        index: dict[str, Client] = {}
        for client in clients:
            email = getattr(client, "email", None)
            if email:
                index[str(email)] = client
        return index

    async def _index(self, *, bypass_cache: bool = False) -> dict[str, Client]:
        """Return the cached index, refreshing it after :data:`CACHE_TTL`."""
        fresh = (
            not bypass_cache
            and self._clients is not None
            and (time.monotonic() - self._cache_at) < CACHE_TTL
        )
        if fresh:
            return self._clients  # type: ignore[return-value]
        index = await self._read_clients()
        self._clients = index
        self._cache_at = time.monotonic()
        return index

    def _invalidate(self) -> None:
        """Drop the cached index (called after every write)."""
        self._clients = None
        self._cache_at = 0.0

    async def _require_client(
        self, tg_id: int, *, bypass_cache: bool = False
    ) -> Client:
        index = await self._index(bypass_cache=bypass_cache)
        client = index.get(str(tg_id))
        if client is None:
            raise ClientNotFound(str(tg_id))
        return client

    async def _write(self, client: Client) -> None:
        """Persist ``client`` (whole-object update) and drop the cache."""
        await self._call(lambda: self._api.client.update(client.id, client))
        self._invalidate()

    # --- reads -------------------------------------------------------------

    async def get_client(self, tg_id: int) -> Client | None:
        """Return the panel client for ``tg_id`` or ``None`` (O(1) from cache)."""
        index = await self._index()
        return index.get(str(tg_id))

    async def list_clients(self) -> list[Client]:
        """Return every client carried by the inbound."""
        return list((await self._index()).values())

    async def get_traffic(self, tg_id: int) -> ClientTraffic | None:
        """Return the traffic snapshot for ``tg_id`` or ``None``."""
        client = await self.get_client(tg_id)
        if client is None:
            return None
        return ClientTraffic(
            email=str(client.email),
            up=int(client.up),
            down=int(client.down),
            total=int(client.total),
            expiry_ms=int(client.expiry_time),
            enable=bool(client.enable),
            sub_id=client.sub_id or "",
        )

    async def server_status(self) -> dict[str, Any]:
        """Return the panel server status payload as a plain dict."""
        server = await self._call(lambda: self._api.server.get_status())
        dump = getattr(server, "model_dump", None)
        if callable(dump):
            return dict(dump())
        return dict(vars(server))

    # --- atomic write primitive -------------------------------------------

    async def mutate(
        self,
        tg_id: int,
        apply: Callable[[Client], bool],
        verify: Callable[[Client], bool],
        *,
        desc: str = "change",
    ) -> Client:
        """Serialise a read-modify-write and verify the result.

        ``apply`` mutates a freshly-read client and returns ``True`` when a
        write is required (``False`` = the caller decided nothing changes, e.g.
        an unlimited client). The write happens under the global lock; the
        changed client is then re-read **bypassing the cache** and checked with
        ``verify`` — a mismatch raises :class:`PanelError` (no silent success).
        """
        async with self._lock:
            client = await self._require_client(tg_id, bypass_cache=True)
            changed = apply(client)
            if changed:
                await self._write(client)
                fresh = await self._require_client(tg_id, bypass_cache=True)
            else:
                fresh = client
        if changed and not verify(fresh):
            raise PanelError(f"panel did not persist {desc} for client {tg_id}")
        return fresh

    # --- writes ------------------------------------------------------------

    async def set_enabled(self, tg_id: int, enabled: bool) -> Client:
        """Enable/disable the client (freeze support)."""
        enabled = bool(enabled)

        def apply(client: Client) -> bool:
            client.enable = enabled
            return True

        return await self.mutate(
            tg_id,
            apply,
            lambda c: bool(c.enable) == enabled,
            desc=f"enable={enabled}",
        )

    async def set_expiry_ms(self, tg_id: int, ms: int) -> Client:
        """Set the absolute expiry timestamp (epoch ms)."""
        ms = int(ms)

        def apply(client: Client) -> bool:
            client.expiry_time = ms
            return True

        return await self.mutate(
            tg_id,
            apply,
            lambda c: int(c.expiry_time) == ms,
            desc=f"expiry_time={ms}",
        )

    async def set_limits(
        self, tg_id: int, total_gb: int | None = None, limit_ip: int | None = None
    ) -> Client:
        """Set traffic quota (GB) and/or IP limit; ``None`` means unlimited."""
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
            desc=f"limits(total_gb={gb}, limit_ip={ips})",
        )

    async def reset_traffic(self, tg_id: int) -> Client:
        """Zero the counters (``up``/``down``); the total stays untouched."""

        def apply(client: Client) -> bool:
            client.up = 0
            client.down = 0
            return True

        return await self.mutate(
            tg_id,
            apply,
            lambda c: int(c.up) == 0 and int(c.down) == 0,
            desc="traffic reset",
        )

    async def ensure_client(self, tg_id: int, username: str = "") -> Client:
        """Return the client for ``tg_id``, creating it if the panel lacks it.

        New clients are created already-expired and disabled with a non-empty
        ``sub_id`` (§2.7). Creation mirrors the legacy ``handlers/start.py``
        flow: ``add`` leaves ``enable=True``, so an immediate ``update`` with
        the same object is required (``docs/DECISIONS.md``).
        """
        email = str(tg_id)
        async with self._lock:
            existing = (await self._index(bypass_cache=True)).get(email)
            if existing is not None:
                return existing

            now_ms = int(time.time() * 1000)
            sub_id = new_sub_id()
            new_client = Client(
                id=str(uuid.uuid4()),
                email=email,
                enable=False,
                expiry_time=now_ms,
                sub_id=sub_id,
                comment=f"@{username}" if username else "",
            )
            await self._call(
                lambda: self._api.client.add(self._inbound_id, [new_client])
            )
            # ``add`` ignores ``enable=False`` — the follow-up update applies it.
            await self._call(lambda: self._api.client.update(new_client.id, new_client))
            self._invalidate()
            fresh = (await self._index(bypass_cache=True)).get(email)
            if fresh is None:
                raise PanelError(f"client {email} missing right after creation")
            if fresh.enable:
                raise PanelError(f"client {email} was not created disabled")
            if not fresh.sub_id:
                raise PanelError(f"client {email} was created without a sub_id")
            return fresh

    async def regenerate_sub_id(self, tg_id: int) -> str:
        """Assign a fresh ``sub_id`` and return it (old link stops working)."""
        sub_id = new_sub_id()

        def apply(client: Client) -> bool:
            client.sub_id = sub_id
            return True

        await self.mutate(
            tg_id,
            apply,
            lambda c: c.sub_id == sub_id,
            desc=f"sub_id={sub_id}",
        )
        return sub_id

    async def delete_client(self, tg_id: int) -> None:
        """Delete the client from the inbound and confirm it is gone."""
        async with self._lock:
            client = await self._require_client(tg_id, bypass_cache=True)
            await self._call(
                lambda: self._api.client.delete(self._inbound_id, client.id)
            )
            self._invalidate()
            index = await self._index(bypass_cache=True)
        if str(tg_id) in index:
            raise PanelError(f"client {tg_id} still present after delete")

    async def restart_xray(self) -> None:
        """Not supported by ``py3xui`` (``docs/panel_api_notes.md`` M0-03)."""
        raise NotSupportedError("Xray restart is not supported by py3xui")

    async def download_panel_db(self, path: str = "") -> None:
        """Not supported by ``py3xui`` (``docs/panel_api_notes.md`` M0-03)."""
        raise NotSupportedError("panel DB export is not supported by py3xui")

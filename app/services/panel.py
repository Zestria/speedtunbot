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

BYTES_PER_GB = 1024**3

# The panel's ``totalGB`` field is measured in **bytes** despite its name
# (S0-2; the unit is unverified until a live smoke run, see
# ``docs/panel_api_notes.md``). This is the single knob to flip to
# ``BYTES_PER_GB`` if the smoke run proves the panel really stores gigabytes.
PANEL_TOTAL_UNIT_BYTES = 1

# Transport-level failures (never a domain error) mapped to PanelUnavailable.
_TRANSPORT_ERRORS: tuple[type[BaseException], ...] = (
    httpx.HTTPError,
    OSError,
    asyncio.TimeoutError,
)


@dataclass(frozen=True)
class ClientTraffic:
    """Traffic snapshot for one panel client (stats merged with settings)."""

    email: str
    up: int
    down: int
    total: int
    expiry_ms: int
    enable: bool
    sub_id: str
    last_online_ms: int | None = None

    @property
    def used(self) -> int:
        """Bytes consumed so far (``up`` + ``down``)."""
        return self.up + self.down


@dataclass(frozen=True)
class _InboundSnapshot:
    """One inbound read: ``settings.clients`` plus per-client traffic stats."""

    clients: dict[str, Client]
    stats: dict[str, Client]


def new_sub_id() -> str:
    """Return a fresh 16-char ``[a-z0-9]`` subscription id (§2.7)."""
    return "".join(random.choices(SUB_ID_ALPHABET, k=SUB_ID_LENGTH))


def gb_to_bytes(gb: int | None) -> int:
    """Convert a GB quota into bytes (``None`` = unlimited → ``0``)."""
    if gb is None:
        return 0
    return max(0, int(gb)) * BYTES_PER_GB


def _to_panel_total(total_bytes: int) -> int:
    """Express ``total_bytes`` in the unit the panel stores in ``totalGB``."""
    return max(0, int(total_bytes)) // PANEL_TOTAL_UNIT_BYTES


def _last_online_ms(stats: Client) -> int | None:
    """Best-effort ``last_online`` (epoch ms) from a stats entry, else ``None``.

    ``py3xui`` drops panel fields it does not model, so this returns ``None``
    whenever the deployed panel does not expose a last-online value (S0-2.3).
    """
    for attr in ("last_online", "lastOnline", "last_online_ms"):
        value = getattr(stats, attr, None)
        if value:
            return int(value)
    return None


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
        #: Per-client traffic stats (``inbound.client_stats``), keyed by email,
        #: refreshed together with ``_clients`` (S0-2.3).
        self._stats: dict[str, Client] = {}
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

    @staticmethod
    def _by_email(entries: Any) -> dict[str, Client]:
        """Index a list of panel clients by their ``email``."""
        index: dict[str, Client] = {}
        for entry in entries or []:
            email = getattr(entry, "email", None)
            if email:
                index[str(email)] = entry
        return index

    async def _read_snapshot(self) -> _InboundSnapshot:
        """Fetch the inbound once: settings clients + per-client traffic stats.

        Live ``up``/``down``/``last_online`` live in ``inbound.client_stats``;
        the quota, expiry and enable flag live in ``inbound.settings.clients``
        (S0-2.3, ``docs/panel_api_notes.md``).
        """
        inbound = await self._call(
            lambda: self._api.inbound.get_by_id(self._inbound_id)
        )
        settings = getattr(inbound, "settings", None)
        return _InboundSnapshot(
            clients=self._by_email(getattr(settings, "clients", None)),
            stats=self._by_email(getattr(inbound, "client_stats", None)),
        )

    async def _index(self, *, bypass_cache: bool = False) -> dict[str, Client]:
        """Return the cached index, refreshing it after :data:`CACHE_TTL`."""
        fresh = (
            not bypass_cache
            and self._clients is not None
            and (time.monotonic() - self._cache_at) < CACHE_TTL
        )
        if fresh:
            return self._clients  # type: ignore[return-value]
        snapshot = await self._read_snapshot()
        self._clients = snapshot.clients
        self._stats = snapshot.stats
        self._cache_at = time.monotonic()
        return self._clients

    def _invalidate(self) -> None:
        """Drop the cached index (called after every write)."""
        self._clients = None
        self._stats = {}
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
        """Return the merged traffic snapshot for ``tg_id`` or ``None``.

        ``up``/``down``/``last_online`` come from ``inbound.client_stats`` when
        the panel reports them; the quota, expiry and enable flag come from the
        settings client. With no stats entry the settings client is used as-is
        (S0-2.3).
        """
        index = await self._index()
        client = index.get(str(tg_id))
        if client is None:
            return None
        stats = self._stats.get(str(tg_id))
        return ClientTraffic(
            email=str(client.email),
            up=int(stats.up) if stats is not None else int(client.up),
            down=int(stats.down) if stats is not None else int(client.down),
            total=int(client.total),
            expiry_ms=int(client.expiry_time),
            enable=bool(client.enable),
            sub_id=client.sub_id or "",
            last_online_ms=_last_online_ms(stats) if stats is not None else None,
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
        self, tg_id: int, total_bytes: int | None = None, limit_ip: int | None = None
    ) -> Client:
        """Set the traffic quota (in **bytes**) and/or IP limit.

        ``None`` means unlimited for either field. The quota is converted into
        the unit the panel stores in ``totalGB`` (bytes, see
        :data:`PANEL_TOTAL_UNIT_BYTES`) — no caller passes raw GB (S0-2.4).
        """
        total = _to_panel_total(0 if total_bytes is None else total_bytes)
        ips = 0 if limit_ip is None else max(0, int(limit_ip))

        def apply(client: Client) -> bool:
            client.total_gb = total
            client.limit_ip = ips
            return True

        return await self.mutate(
            tg_id,
            apply,
            lambda c: int(c.total_gb) == total and int(c.limit_ip) == ips,
            desc=f"limits(total_bytes={total_bytes}, limit_ip={ips})",
        )

    async def reset_traffic(self, tg_id: int) -> Client:
        """Zero one client's counters via the panel's dedicated reset route.

        Prefers ``py3xui``'s ``client.reset_stats``; falls back to a raw
        ``POST /panel/api/inbounds/{id}/resetClientTraffic/{email}`` when the
        wrapper is absent (S0-2.5). The reset runs under the write lock and is
        verified by re-reading the stats (``up == down == 0``).
        """
        email = str(tg_id)
        async with self._lock:
            await self._require_client(tg_id, bypass_cache=True)
            await self._reset_stats(email)
            self._invalidate()
            index = await self._index(bypass_cache=True)
        client = index.get(email)
        if client is None:
            raise ClientNotFound(str(tg_id))
        stats = self._stats.get(email)
        up = int(stats.up) if stats is not None else int(client.up)
        down = int(stats.down) if stats is not None else int(client.down)
        if up != 0 or down != 0:
            raise PanelError(f"panel did not reset traffic for client {tg_id}")
        return client

    async def _reset_stats(self, email: str) -> None:
        """Reset ``email``'s counters (py3xui wrapper, else the raw route)."""
        reset = getattr(self._api.client, "reset_stats", None)
        if callable(reset):
            await self._call(lambda: reset(self._inbound_id, email))
            return
        await self._call(lambda: self._reset_stats_raw(email))

    async def _reset_stats_raw(self, email: str) -> None:
        """Raw fallback: ``POST …/resetClientTraffic/{email}`` with the token.

        The URL is built from ``settings.domain`` exactly, so a panel deployed
        behind a base path keeps it (``docs/panel_api_notes.md`` §1).
        """
        base = self._settings.domain.rstrip("/")
        url = f"{base}/panel/api/inbounds/{self._inbound_id}/resetClientTraffic/{email}"
        headers = {
            "Authorization": f"Bearer {self._settings.vpn_token}",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.post(url, headers=headers)
            response.raise_for_status()

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
        """Assign a fresh ``sub_id`` and return it (old link stops working).

        Hardened per §S1-5.0: a write that produced an empty id, or silently
        kept the previous one, is a failure (``PanelError``) rather than a
        "success" that would hand the user an unchanged link.
        """
        sub_id = new_sub_id()
        if not sub_id:  # pragma: no cover - ``new_sub_id`` is random, not empty
            raise PanelError("panel produced an empty sub_id")
        previous: list[str] = []

        def apply(client: Client) -> bool:
            previous.append(client.sub_id or "")
            client.sub_id = sub_id
            return True

        fresh = await self.mutate(
            tg_id,
            apply,
            lambda c: c.sub_id == sub_id,
            desc=f"sub_id={sub_id}",
        )
        if not fresh.sub_id or fresh.sub_id == (previous[0] if previous else ""):
            raise PanelError(f"panel did not change the sub_id of client {tg_id}")
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
        """Restart the panel's Xray service (§S2-5.1).

        ``py3xui`` 0.7.0 has no wrapper, so the confirmed 3x-ui route
        (``POST /panel/api/server/restartXrayService``) is called directly in
        token mode — the same raw-HTTP style :meth:`_reset_stats_raw` uses
        (``docs/panel_api_notes.md`` §1, Q6). This **replaces** the earlier
        ``NotSupportedError`` decision, but is still **never** called by bot
        logic on its own: only the owner's confirmed button reaches it.
        """
        await self._call(lambda: self._restart_xray_raw())

    async def _restart_xray_raw(self) -> None:
        """Raw restart route, mirroring :meth:`_reset_stats_raw`."""
        base = self._settings.domain.rstrip("/")
        url = f"{base}/panel/api/server/restartXrayService"
        headers = {
            "Authorization": f"Bearer {self._settings.vpn_token}",
            "Accept": "application/json",
        }
        async with httpx.AsyncClient(timeout=self._timeout) as http:
            response = await http.post(url, headers=headers)
            response.raise_for_status()

    async def download_panel_db(self, path: str = "") -> None:
        """Not supported by ``py3xui`` (``docs/panel_api_notes.md`` M0-03)."""
        raise NotSupportedError("panel DB export is not supported by py3xui")

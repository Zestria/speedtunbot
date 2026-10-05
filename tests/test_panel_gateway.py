"""``PanelGateway`` tests against a stubbed ``py3xui`` API (§M0-04)."""

from __future__ import annotations

import asyncio
from types import SimpleNamespace

import httpx
import pytest
from py3xui import Client

from app.errors import ClientNotFound, NotSupportedError, PanelError, PanelUnavailable
from app.services.panel import PanelGateway
from app.settings import Settings

TG = 1001


class StubApi:
    """Minimal stand-in for ``py3xui.AsyncApi`` (token mode)."""

    def __init__(self) -> None:
        self.clients: dict[str, Client] = {}
        self.apply_updates = True
        self.error: Exception | None = None
        self.delay = 0.0
        self.inbound_reads = 0
        self.inbound = SimpleNamespace(get_by_id=self._get_by_id)
        self.client = SimpleNamespace(
            add=self._add, update=self._update, delete=self._delete
        )
        self.server = SimpleNamespace(get_status=self._get_status)

    async def _pre(self) -> None:
        if self.delay:
            await asyncio.sleep(self.delay)
        if self.error is not None:
            raise self.error

    async def _get_by_id(self, inbound_id: int) -> object:
        await self._pre()
        self.inbound_reads += 1
        # Deep-copy like a real HTTP round-trip: the gateway must not be able
        # to mutate panel state by accident.
        return SimpleNamespace(
            settings=SimpleNamespace(
                clients=[c.model_copy(deep=True) for c in self.clients.values()]
            )
        )

    async def _add(self, inbound_id: int, clients: list[Client]) -> None:
        await self._pre()
        for client in clients:
            # The real panel ignores ``enable=False`` on add (M0-03 notes).
            stored = client.model_copy(deep=True)
            stored.enable = True
            self.clients[stored.email] = stored

    async def _update(self, client_id: str, client: Client) -> None:
        await self._pre()
        if not self.apply_updates:
            return  # silently drops the change -> gateway must notice
        self.clients[client.email] = client.model_copy(deep=True)

    async def _delete(self, inbound_id: int, client_id: str) -> None:
        await self._pre()
        for email, client in list(self.clients.items()):
            if client.id == client_id:
                del self.clients[email]

    async def _get_status(self) -> object:
        await self._pre()
        return SimpleNamespace(model_dump=lambda: {"cpu": 3.0, "online": 1})


def make_gateway(settings: Settings, api: StubApi, **kwargs: object) -> PanelGateway:
    return PanelGateway(settings, api=api, **kwargs)  # type: ignore[arg-type]


def seed(api: StubApi, tg_id: int = TG, **fields: object) -> Client:
    fields.setdefault("enable", False)
    client = Client(id="uuid-1", email=str(tg_id), **fields)  # type: ignore[arg-type]
    api.clients[str(tg_id)] = client
    return client


async def test_ensure_client_creates_disabled_expired_client(
    settings: Settings,
) -> None:
    api = StubApi()
    gateway = make_gateway(settings, api)

    client = await gateway.ensure_client(TG, "vasya")

    assert client.enable is False  # the add/update workaround applied
    assert client.expiry_time > 0
    assert client.sub_id and len(client.sub_id) == 16
    assert client.comment == "@vasya"
    assert api.clients[str(TG)].enable is False


async def test_ensure_client_is_idempotent(settings: Settings) -> None:
    api = StubApi()
    gateway = make_gateway(settings, api)
    first = await gateway.ensure_client(TG, "vasya")
    second = await gateway.ensure_client(TG, "vasya")
    assert first.sub_id == second.sub_id
    assert len(api.clients) == 1


async def test_ensure_client_requires_disabled_and_sub_id(settings: Settings) -> None:
    api = StubApi()
    api.apply_updates = False  # panel keeps enable=True -> gateway must fail
    gateway = make_gateway(settings, api)

    with pytest.raises(PanelError):
        await gateway.ensure_client(TG, "vasya")


async def test_set_enabled_verifies_after_write(settings: Settings) -> None:
    api = StubApi()
    seed(api, enable=True)
    gateway = make_gateway(settings, api)

    client = await gateway.set_enabled(TG, False)

    assert client.enable is False
    assert api.clients[str(TG)].enable is False


async def test_write_that_is_dropped_raises_panel_error(settings: Settings) -> None:
    api = StubApi()
    seed(api, enable=True)
    api.apply_updates = False
    gateway = make_gateway(settings, api)

    with pytest.raises(PanelError, match="did not persist"):
        await gateway.set_enabled(TG, False)


async def test_get_client_is_cached_until_write(settings: Settings) -> None:
    api = StubApi()
    seed(api)
    gateway = make_gateway(settings, api)

    await gateway.get_client(TG)
    await gateway.get_client(TG)
    assert api.inbound_reads == 1  # second read served from the cache

    await gateway.set_enabled(TG, True)
    await gateway.get_client(TG)
    assert api.inbound_reads > 2  # write invalidated the cache


async def test_transport_error_becomes_panel_unavailable(settings: Settings) -> None:
    api = StubApi()
    api.error = httpx.ConnectError("boom")
    gateway = make_gateway(settings, api)

    with pytest.raises(PanelUnavailable):
        await gateway.get_client(TG)


async def test_timeout_becomes_panel_unavailable(settings: Settings) -> None:
    api = StubApi()
    api.delay = 0.2
    gateway = make_gateway(settings, api, timeout=0.01)

    with pytest.raises(PanelUnavailable):
        await gateway.get_client(TG)


async def test_concurrent_writes_serialise(settings: Settings) -> None:
    api = StubApi()
    seed(api, up=0)
    gateway = make_gateway(settings, api)

    def increment(client: Client) -> bool:
        client.up = int(client.up) + 1
        return True

    await asyncio.gather(
        gateway.mutate(TG, increment, lambda c: True),
        gateway.mutate(TG, increment, lambda c: True),
    )

    client = await gateway.get_client(TG)
    assert client is not None and client.up == 2  # no lost update


async def test_limits_and_reset_traffic(settings: Settings) -> None:
    api = StubApi()
    seed(api, up=500, down=700, total_gb=0)
    gateway = make_gateway(settings, api)

    limited = await gateway.set_limits(TG, total_gb=50, limit_ip=2)
    assert (limited.total_gb, limited.limit_ip) == (50, 2)

    unlimited = await gateway.set_limits(TG, total_gb=None, limit_ip=None)
    assert (unlimited.total_gb, unlimited.limit_ip) == (0, 0)

    reset = await gateway.reset_traffic(TG)
    assert (reset.up, reset.down) == (0, 0)


async def test_get_traffic_snapshot(settings: Settings) -> None:
    api = StubApi()
    seed(api, up=10, down=20, total=1000, expiry_time=555, enable=True, sub_id="abc")
    gateway = make_gateway(settings, api)

    traffic = await gateway.get_traffic(TG)
    assert traffic is not None
    assert (traffic.up, traffic.down, traffic.total) == (10, 20, 1000)
    assert traffic.used == 30
    assert traffic.expiry_ms == 555
    assert traffic.sub_id == "abc"
    assert await gateway.get_traffic(9999) is None


async def test_regenerate_sub_id_changes_it(settings: Settings) -> None:
    api = StubApi()
    seed(api, sub_id="old-sub-id")
    gateway = make_gateway(settings, api)

    new = await gateway.regenerate_sub_id(TG)

    assert new != "old-sub-id" and len(new) == 16
    client = await gateway.get_client(TG)
    assert client is not None and client.sub_id == new


async def test_delete_client_removes_it(settings: Settings) -> None:
    api = StubApi()
    seed(api)
    gateway = make_gateway(settings, api)

    await gateway.delete_client(TG)

    assert await gateway.get_client(TG) is None
    with pytest.raises(ClientNotFound):
        await gateway.delete_client(TG)


async def test_server_status_returns_dict(settings: Settings) -> None:
    gateway = make_gateway(settings, StubApi())
    status = await gateway.server_status()
    assert status["cpu"] == 3.0


async def test_unsupported_operations_raise(settings: Settings) -> None:
    gateway = make_gateway(settings, StubApi())
    with pytest.raises(NotSupportedError):
        await gateway.restart_xray()
    with pytest.raises(NotSupportedError):
        await gateway.download_panel_db()

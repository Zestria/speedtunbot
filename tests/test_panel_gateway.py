"""``PanelGateway`` tests against a stubbed ``py3xui`` API (§M0-04)."""

from __future__ import annotations

import asyncio
from collections.abc import Callable
from types import SimpleNamespace

import httpx
import pytest
from py3xui import Client

from app.errors import ClientNotFound, NotSupportedError, PanelError, PanelUnavailable
from app.services import panel as panel_mod
from app.services.panel import PanelGateway, gb_to_bytes
from app.settings import Settings

TG = 1001


class StubApi:
    """Minimal stand-in for ``py3xui.AsyncApi`` (token mode).

    Settings clients (``inbound.settings.clients``) and per-client traffic
    stats (``inbound.client_stats``) are kept in two separate maps, exactly
    like the deployed panel (S0-2.3).
    """

    def __init__(self) -> None:
        self.clients: dict[str, Client] = {}
        self.stats: dict[str, Client] = {}
        self.apply_updates = True
        self.error: Exception | None = None
        self.delay = 0.0
        self.inbound_reads = 0
        self.resets: list[str] = []
        self.inbound = SimpleNamespace(get_by_id=self._get_by_id)
        self.client = SimpleNamespace(
            add=self._add,
            update=self._update,
            delete=self._delete,
            reset_stats=self._reset_stats,
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
            ),
            client_stats=[c.model_copy(deep=True) for c in self.stats.values()],
        )

    async def _add(self, inbound_id: int, clients: list[Client]) -> None:
        await self._pre()
        for client in clients:
            # The real panel ignores ``enable=False`` on add (M0-03 notes).
            stored = client.model_copy(deep=True)
            stored.enable = True
            self.clients[stored.email] = stored
            self.stats[stored.email] = stored.model_copy(deep=True)

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
                self.stats.pop(email, None)

    async def _reset_stats(self, inbound_id: int, email: str) -> None:
        await self._pre()
        self.resets.append(email)
        stats = self.stats.get(email)
        if stats is not None:
            stats.up = 0
            stats.down = 0

    async def _get_status(self) -> object:
        await self._pre()
        return SimpleNamespace(model_dump=lambda: {"cpu": 3.0, "online": 1})


def make_gateway(settings: Settings, api: StubApi, **kwargs: object) -> PanelGateway:
    return PanelGateway(settings, api=api, **kwargs)  # type: ignore[arg-type]


def seed(
    api: StubApi,
    tg_id: int = TG,
    *,
    up: int = 0,
    down: int = 0,
    **fields: object,
) -> Client:
    """Seed a settings client plus a matching stats entry."""
    fields.setdefault("enable", False)
    client = Client(id="uuid-1", email=str(tg_id), up=up, down=down, **fields)
    api.clients[str(tg_id)] = client
    api.stats[str(tg_id)] = Client(
        id="uuid-1", email=str(tg_id), enable=True, up=up, down=down
    )
    return client


def recording_http_client(
    calls: list[tuple[str, dict[str, str]]],
    *,
    error: Exception | None = None,
    on_post: Callable[[str], None] | None = None,
) -> type:
    """Build an ``httpx.AsyncClient`` stand-in that records raw POSTs."""

    class RecordingHttpClient:
        def __init__(self, **kwargs: object) -> None:
            self.kwargs = kwargs

        async def __aenter__(self) -> RecordingHttpClient:
            return self

        async def __aexit__(self, *exc: object) -> bool:
            return False

        async def post(
            self, url: str, *, headers: dict[str, str], **kwargs: object
        ) -> httpx.Response:
            calls.append((url, dict(headers)))
            if on_post is not None:
                on_post(url)
            if error is not None:
                raise error
            return httpx.Response(200, request=httpx.Request("POST", url))

    return RecordingHttpClient


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


async def test_set_limits_writes_bytes(settings: Settings) -> None:
    api = StubApi()
    seed(api, up=500, down=700, total_gb=0)
    gateway = make_gateway(settings, api)

    limited = await gateway.set_limits(TG, total_bytes=1024**3, limit_ip=2)
    assert (limited.total_gb, limited.limit_ip) == (1073741824, 2)

    unlimited = await gateway.set_limits(TG, total_bytes=None, limit_ip=None)
    assert (unlimited.total_gb, unlimited.limit_ip) == (0, 0)


async def test_gb_to_bytes_and_panel_total_helpers() -> None:
    assert gb_to_bytes(1) == 1024**3
    assert gb_to_bytes(None) == 0
    assert panel_mod._to_panel_total(1024**3) == 1024**3


async def test_reset_traffic_uses_the_panel_reset(settings: Settings) -> None:
    api = StubApi()
    seed(api, up=500, down=700)
    gateway = make_gateway(settings, api)

    await gateway.reset_traffic(TG)

    assert api.resets == [str(TG)]
    traffic = await gateway.get_traffic(TG)
    assert traffic is not None and (traffic.up, traffic.down) == (0, 0)


async def test_reset_traffic_raw_fallback(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = StubApi()
    seed(api, up=500, down=700)
    # No py3xui wrapper -> the gateway must use the raw reset route.
    api.client = SimpleNamespace(add=api._add, update=api._update, delete=api._delete)
    panel_settings = settings.model_copy(
        update={"domain": "https://panel.example.com/xui/"}
    )
    gateway = make_gateway(panel_settings, api)

    calls: list[tuple[str, dict[str, str]]] = []

    def zero(_url: str) -> None:
        stats = api.stats[str(TG)]
        stats.up = 0
        stats.down = 0

    monkeypatch.setattr(
        panel_mod.httpx, "AsyncClient", recording_http_client(calls, on_post=zero)
    )

    await gateway.reset_traffic(TG)

    url, headers = calls[0]
    assert url == (
        "https://panel.example.com/xui/panel/api/inbounds/1/resetClientTraffic/1001"
    )
    assert headers["Authorization"] == "Bearer vpn-token-value"
    traffic = await gateway.get_traffic(TG)
    assert traffic is not None and traffic.used == 0


async def test_reset_traffic_raw_fallback_maps_transport_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    api = StubApi()
    seed(api, up=1, down=1)
    api.client = SimpleNamespace(add=api._add, update=api._update, delete=api._delete)
    gateway = make_gateway(settings, api)

    monkeypatch.setattr(
        panel_mod.httpx,
        "AsyncClient",
        recording_http_client([], error=httpx.ConnectError("boom")),
    )

    with pytest.raises(PanelUnavailable):
        await gateway.reset_traffic(TG)


async def test_get_traffic_prefers_client_stats(settings: Settings) -> None:
    api = StubApi()
    seed(api, up=1, down=1, total=1000, expiry_time=555, enable=True, sub_id="abc")
    # The panel's live counters differ from the settings copy.
    api.stats[str(TG)].up = 100
    api.stats[str(TG)].down = 200
    gateway = make_gateway(settings, api)

    traffic = await gateway.get_traffic(TG)
    assert traffic is not None
    assert (traffic.up, traffic.down) == (100, 200)
    assert traffic.total == 1000  # the quota still comes from the settings
    assert traffic.expiry_ms == 555
    assert traffic.enable is True
    assert traffic.last_online_ms is None


async def test_get_traffic_falls_back_to_settings_client(settings: Settings) -> None:
    api = StubApi()
    seed(api, up=7, down=8, enable=True)
    api.stats.clear()  # the panel reports no stats for this client
    gateway = make_gateway(settings, api)

    traffic = await gateway.get_traffic(TG)
    assert traffic is not None and (traffic.up, traffic.down) == (7, 8)
    assert traffic.last_online_ms is None


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
    assert traffic.last_online_ms is None
    assert await gateway.get_traffic(9999) is None


async def test_regenerate_sub_id_changes_it(settings: Settings) -> None:
    api = StubApi()
    seed(api, sub_id="old-sub-id")
    gateway = make_gateway(settings, api)

    new = await gateway.regenerate_sub_id(TG)

    assert new != "old-sub-id" and len(new) == 16
    client = await gateway.get_client(TG)
    assert client is not None and client.sub_id == new


async def test_regenerate_sub_id_rejects_a_silent_noop(settings: Settings) -> None:
    """§S1-5.0: a write that kept the old ``sub_id`` is a failure, not success."""
    api = StubApi()
    seed(api, sub_id="old-sub-id")
    api.apply_updates = False  # the panel "accepts" the update and drops it
    gateway = make_gateway(settings, api)

    with pytest.raises(PanelError):
        await gateway.regenerate_sub_id(TG)
    client = await gateway.get_client(TG)
    assert client is not None and client.sub_id == "old-sub-id"


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


async def test_download_panel_db_is_unsupported(settings: Settings) -> None:
    gateway = make_gateway(settings, StubApi())
    with pytest.raises(NotSupportedError):
        await gateway.download_panel_db()


async def test_restart_xray_posts_the_confirmed_route(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    """§S2-5.1: the raw ``restartXrayService`` route with the bearer token."""
    gateway = make_gateway(settings, StubApi())
    calls: list[tuple[str, dict[str, str]]] = []
    monkeypatch.setattr(panel_mod.httpx, "AsyncClient", recording_http_client(calls))

    await gateway.restart_xray()

    url, headers = calls[0]
    assert url == "https://panel.example.com/panel/api/server/restartXrayService"
    assert headers["Authorization"] == "Bearer vpn-token-value"


async def test_restart_xray_maps_a_transport_error(
    settings: Settings, monkeypatch: pytest.MonkeyPatch
) -> None:
    gateway = make_gateway(settings, StubApi())
    monkeypatch.setattr(
        panel_mod.httpx,
        "AsyncClient",
        recording_http_client([], error=httpx.ConnectError("boom")),
    )

    with pytest.raises(PanelUnavailable):
        await gateway.restart_xray()

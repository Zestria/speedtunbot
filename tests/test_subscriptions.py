"""Subscription math + grant tests (``TASK_PLAN.md`` §M0-04)."""

from __future__ import annotations

import asyncio

import pytest

from app.errors import ClientNotFound, PanelError
from app.services.subscriptions import (
    MS_PER_DAY,
    UNLIMITED_REASON,
    GrantResult,
    SubscriptionService,
    calculate_expiry_ms,
    now_ms,
)
from tests.fakes import FakePanel

NOW = 1_700_000_000_000


@pytest.mark.parametrize(
    ("old_ms", "days", "expected"),
    [
        (None, 30, NOW + 30 * MS_PER_DAY),  # unknown -> counts from now
        (0, 30, 0),  # perpetual -> untouched
        (-1, 7, NOW + 7 * MS_PER_DAY),  # delayed start marker
        (NOW - 5 * MS_PER_DAY, 30, NOW + 30 * MS_PER_DAY),  # expired -> from now
        (NOW + 3 * MS_PER_DAY, 30, NOW + 33 * MS_PER_DAY),  # active -> extend
        (NOW, 0, NOW),  # zero-day grant is a no-op
    ],
)
def test_calculate_expiry_ms(old_ms: int | None, days: int, expected: int) -> None:
    assert calculate_expiry_ms(old_ms, days, NOW) == expected


async def test_grant_days_extends_active_client() -> None:
    panel = FakePanel()
    start = now_ms() + 3 * MS_PER_DAY
    panel.seed(1, expiry_ms=start)
    service = SubscriptionService(panel)  # type: ignore[arg-type]

    result = await service.grant_days(1, 30, actor=42)

    assert result == GrantResult(1, start + 30 * MS_PER_DAY, changed=True)
    client = await panel.get_client(1)
    assert client is not None and client.expiry_time == start + 30 * MS_PER_DAY


async def test_grant_days_restarts_expired_client() -> None:
    panel = FakePanel()
    panel.seed(1, expiry_ms=now_ms() - MS_PER_DAY, enable=False)
    service = SubscriptionService(panel)  # type: ignore[arg-type]

    before = now_ms()
    await service.grant_days(1, 30)
    after = now_ms()

    client = await panel.get_client(1)
    assert client is not None
    assert before + 30 * MS_PER_DAY <= client.expiry_time <= after + 30 * MS_PER_DAY


async def test_grant_days_on_unlimited_client_is_a_no_op() -> None:
    panel = FakePanel()
    panel.seed(1, expiry_ms=0)
    service = SubscriptionService(panel)  # type: ignore[arg-type]

    result = await service.grant_days(1, 30)

    assert result.changed is False
    assert result.reason == UNLIMITED_REASON
    assert result.expiry_ms == 0
    client = await panel.get_client(1)
    assert client is not None and client.expiry_time == 0
    assert "mutate" in panel.calls  # verification path still consulted the panel


async def test_concurrent_grants_add_up() -> None:
    panel = FakePanel()
    start = now_ms() + 100 * MS_PER_DAY
    panel.seed(1, expiry_ms=start)
    service = SubscriptionService(panel)  # type: ignore[arg-type]

    await asyncio.gather(service.grant_days(1, 5), service.grant_days(1, 5))

    client = await panel.get_client(1)
    assert client is not None
    assert client.expiry_time == start + 10 * MS_PER_DAY


async def test_grant_days_unknown_client_raises() -> None:
    service = SubscriptionService(FakePanel())  # type: ignore[arg-type]
    with pytest.raises(ClientNotFound):
        await service.grant_days(777, 30)


async def test_set_expiry_sets_absolute_value() -> None:
    panel = FakePanel()
    panel.seed(1)
    service = SubscriptionService(panel)  # type: ignore[arg-type]

    target = now_ms() + 10 * MS_PER_DAY
    result = await service.set_expiry(1, target, actor=1)

    assert result.expiry_ms == target
    client = await panel.get_client(1)
    assert client is not None and client.expiry_time == target


async def test_freeze_toggles_enable() -> None:
    panel = FakePanel()
    panel.seed(1, enable=True)
    service = SubscriptionService(panel)  # type: ignore[arg-type]

    frozen = await service.freeze(1, True)
    assert frozen.enable is False

    thawed = await service.freeze(1, False)
    assert thawed.enable is True


async def test_grant_days_propagates_verify_failure() -> None:
    panel = FakePanel()
    panel.seed(1, expiry_ms=now_ms() + MS_PER_DAY)
    panel.drop_writes = True
    service = SubscriptionService(panel)  # type: ignore[arg-type]

    with pytest.raises(PanelError):
        await service.grant_days(1, 30)

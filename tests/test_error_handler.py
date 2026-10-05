"""Global error-handling tests (``TASK_PLAN.md`` §M0-08.3 / §M0-08.4).

Covers the acceptance criteria: an exception produces exactly **one** log record
and at most **one** owner alert per window. The last middleware test drives a
real ``AsyncTeleBot`` dispatcher so telebot's own exception plumbing (not just
our direct call into the middleware) is exercised — telebot swallows handler
exceptions, so ``post_process`` is the only observation point.
"""

from __future__ import annotations

import html
import logging
from collections.abc import Callable

import pytest
import pytest_asyncio
from sqlalchemy.ext.asyncio import AsyncEngine, AsyncSession, async_sessionmaker
from telebot.async_telebot import AsyncTeleBot
from telebot.asyncio_handler_backends import CancelUpdate
from telebot.types import Update

from app.db.base import JSONText
from app.error_handler import (
    ErrorReporter,
    ErrorReporterMiddleware,
    error_signature,
    run_polling,
)
from app.logging_setup import MASK
from app.services.admins import AdminService
from app.services.notifier import Notifier
from app.settings import Settings
from app.utils.pagination import clamp_page, paginate
from tests.fakes import FakeBot

OWNER = 1


@pytest_asyncio.fixture
async def factory(engine: AsyncEngine) -> async_sessionmaker[AsyncSession]:
    """Sessionmaker over the shared in-memory engine."""
    return async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)


def _raise_a() -> BaseException:
    """Raise and return an exception (so it carries a real traceback)."""
    try:
        raise ValueError("boom")
    except ValueError as exc:
        return exc


def _raise_b() -> BaseException:
    """Same exception type/message, different raise site ⇒ other signature."""
    try:
        raise ValueError("boom")
    except ValueError as exc:
        return exc


def _boom(message: str) -> BaseException:
    try:
        raise ValueError(message)
    except ValueError as exc:
        return exc


def _captured(flow: Callable[[], object]) -> BaseException:
    """Run ``flow`` and return the exception it raised (with its traceback)."""
    try:
        flow()
    except BaseException as exc:
        return exc
    raise AssertionError("flow did not raise")


def _call_int(value: object) -> None:
    """One shared library call: the failing frame is this one (``int`` is C)."""
    int(value)  # type: ignore[call-overload]


def _reporter(
    settings: Settings, factory: async_sessionmaker[AsyncSession], bot: FakeBot
) -> ErrorReporter:
    return ErrorReporter(Notifier(AdminService(settings, factory), bot=bot))


def _command_update(text: str, update_id: int = 1) -> Update:
    return Update.de_json(
        {
            "update_id": update_id,
            "message": {
                "message_id": update_id,
                "date": 0,
                "chat": {"id": 5, "type": "private"},
                "from": {"id": 7, "is_bot": False, "first_name": "N"},
                "text": text,
                "entities": [{"type": "bot_command", "offset": 0, "length": len(text)}],
            },
        }
    )


# --- signature -------------------------------------------------------------


def test_error_signature_is_stable_and_call_site_specific() -> None:
    first = error_signature(_raise_a())
    assert error_signature(_raise_a()) == first
    assert error_signature(_raise_b()) != first
    assert first.startswith("ValueError:")


def test_error_signature_includes_the_exception_type() -> None:
    # The same library call from the same frame: ``int`` is a C function, so no
    # library frame exists and *only* the exception type can separate the two.
    value_error = _captured(lambda: _call_int("not a number"))
    type_error = _captured(lambda: _call_int(None))

    assert type(value_error).__name__ == "ValueError"
    assert type(type_error).__name__ == "TypeError"
    assert error_signature(value_error) != error_signature(type_error)
    assert error_signature(value_error).startswith("ValueError:")
    assert error_signature(type_error).startswith("TypeError:")


def test_error_signature_prefers_the_application_frame_over_the_library_frame() -> None:
    # ``json.loads`` raises inside ``json.decoder``: every caller of it would
    # share one signature, so the innermost *application* frame must win.
    exc = _captured(lambda: JSONText().process_result_value("[not json", None))

    assert type(exc).__name__ == "JSONDecodeError"
    signature = error_signature(exc)
    assert signature.startswith("JSONDecodeError:app.db.base:process_result_value:")
    assert "json.decoder" not in signature


def test_error_signature_separates_app_callers_of_one_library_call() -> None:
    # Two application flows failing in the same shared call (``int``), with the
    # same exception type, must still get distinct signatures.
    paged = _captured(lambda: paginate([1, 2, 3], "boom"))
    clamped = _captured(lambda: clamp_page("boom", 3))

    assert type(paged).__name__ == type(clamped).__name__ == "ValueError"
    first = error_signature(paged)
    second = error_signature(clamped)
    assert first != second
    assert first.startswith("ValueError:app.utils.pagination:paginate:")
    assert second.startswith("ValueError:app.utils.pagination:clamp_page:")


# --- report: one log entry, at most one alert ------------------------------


async def test_report_logs_traceback_and_alerts_owners(
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = FakeBot()
    reporter = _reporter(settings, factory, bot)

    with caplog.at_level(logging.ERROR):
        assert await reporter.report(_raise_a(), context="handler") is True

    records = [r for r in caplog.records if r.name == "app.error_handler"]
    assert len(records) == 1
    assert records[0].exc_info is not None  # the traceback is logged
    assert "unhandled handler error" in records[0].getMessage()

    assert len(bot.sent) == 1
    assert bot.sent[0][0] == OWNER


async def test_second_identical_error_is_suppressed_within_the_window(
    settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> None:
    clock = [1000.0]
    bot = FakeBot()
    notifier = Notifier(
        AdminService(settings, factory), bot=bot, clock=lambda: clock[0]
    )
    reporter = ErrorReporter(notifier)

    assert await reporter.report(_raise_a(), context="handler") is True
    assert await reporter.report(_raise_a(), context="handler") is False
    assert len(bot.sent) == 1

    # A different bug site is a different signature: alerted immediately.
    assert await reporter.report(_raise_b(), context="handler") is True
    assert len(bot.sent) == 2

    # After the window the first signature may alert again.
    clock[0] += 601.0
    assert await reporter.report(_raise_a(), context="handler") is True
    assert len(bot.sent) == 3


async def test_alert_is_html_escaped_and_secret_masked(
    settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> None:
    bot = FakeBot()
    notifier = Notifier(AdminService(settings, factory), bot=bot)
    reporter = ErrorReporter(notifier, secrets=[settings.bot_token])

    exc = _boom(f"failed with {settings.bot_token} <b>x</b>")
    assert await reporter.report(exc) is True

    _, text = bot.sent[0]
    assert settings.bot_token not in text
    assert MASK in text
    assert "<b>x</b>" not in text  # only our own markup survives escaping
    assert "&lt;b&gt;x&lt;/b&gt;" in text


async def test_alert_masks_secrets_before_html_escaping(
    settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> None:
    """Escaping first would break masking: ``a&b`` no longer contains ``a&b``."""
    secret = "tok&<en>12345"
    bot = FakeBot()
    notifier = Notifier(AdminService(settings, factory), bot=bot)
    reporter = ErrorReporter(notifier, secrets=[secret])

    assert await reporter.report(_boom(f"login failed for {secret} <i>")) is True

    _, text = bot.sent[0]
    assert secret not in text
    assert html.escape(secret) not in text  # proves masking ran *first*
    assert MASK in text
    assert "&lt;i&gt;" in text  # the rest is still escaped


async def test_report_without_notifier_only_logs(
    caplog: pytest.LogCaptureFixture,
) -> None:
    with caplog.at_level(logging.ERROR):
        assert await ErrorReporter().report(_raise_a()) is False
    assert len([r for r in caplog.records if r.name == "app.error_handler"]) == 1


async def test_report_survives_a_broken_transport(
    settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> None:
    class ExplodingBot:
        async def send_message(self, *args: object, **kwargs: object) -> object:
            raise RuntimeError("telegram down")

    notifier = Notifier(AdminService(settings, factory), bot=ExplodingBot())
    reporter = ErrorReporter(notifier)
    # ``safe_send`` swallows transport errors, so the alert path cannot raise:
    # the report still completes and reports whether the alert "went out".
    assert await reporter.report(_raise_a()) is True


# --- middleware ------------------------------------------------------------


async def test_middleware_reports_only_real_exceptions(
    settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> None:
    bot = FakeBot()
    middleware = ErrorReporterMiddleware(_reporter(settings, factory, bot))
    assert middleware.update_types == ["message", "callback_query"]

    assert await middleware.pre_process(object(), {}) is None
    # A clean update must not alert.
    await middleware.post_process(object(), {}, None)
    assert bot.sent == []

    await middleware.post_process(object(), {}, RuntimeError("kaboom"))
    assert len(bot.sent) == 1
    assert "kaboom" in bot.sent[0][1]


async def test_middleware_ignores_cancel_update(
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    """A dropped update is control flow, not an error: no log, no owner alert."""
    bot = FakeBot()
    middleware = ErrorReporterMiddleware(_reporter(settings, factory, bot))

    with caplog.at_level(logging.DEBUG):
        await middleware.post_process(object(), {}, CancelUpdate())

    assert [r for r in caplog.records if r.name == "app.error_handler"] == []
    assert bot.sent == []


async def test_handler_exception_through_telebot_alerts_once(
    settings: Settings,
    factory: async_sessionmaker[AsyncSession],
    caplog: pytest.LogCaptureFixture,
) -> None:
    bot = FakeBot()
    dispatcher = AsyncTeleBot("123456:AA-test-token-value")
    dispatcher.setup_middleware(
        ErrorReporterMiddleware(_reporter(settings, factory, bot))
    )

    @dispatcher.message_handler(commands=["boom"])
    async def _boom_handler(message: object) -> None:
        raise ValueError("handler blew up")

    with caplog.at_level(logging.ERROR):
        await dispatcher.process_new_updates([_command_update("/boom", 1)])
        await dispatcher.process_new_updates([_command_update("/boom", 2)])

    # Two identical handler failures ⇒ two log entries, one owner alert.
    assert len([r for r in caplog.records if r.name == "app.error_handler"]) == 2
    assert len(bot.sent) == 1
    assert "handler blew up" in bot.sent[0][1]


# --- polling ---------------------------------------------------------------


async def test_run_polling_reports_and_reraises(
    settings: Settings, factory: async_sessionmaker[AsyncSession]
) -> None:
    bot = FakeBot()
    reporter = _reporter(settings, factory, bot)

    class DyingBot:
        async def polling(self, **kwargs: object) -> None:
            raise RuntimeError("polling died")

    with pytest.raises(RuntimeError, match="polling died"):
        await run_polling(DyingBot(), reporter)

    assert len(bot.sent) == 1
    assert "polling died" in bot.sent[0][1]
    assert "Контекст:" in bot.sent[0][1]

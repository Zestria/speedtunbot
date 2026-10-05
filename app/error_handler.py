"""Global error handling (``TASK_PLAN.md`` §M0-08.2 / §M0-08.3).

Two entry points share one :class:`ErrorReporter`:

* :class:`ErrorReporterMiddleware` — telebot's ``post_process`` hook. Telebot
  swallows handler exceptions (it logs them itself and keeps polling), so
  ``post_process`` is the **only** place a handler failure can be observed.
  Telebot control-flow objects such as
  :class:`~telebot.asyncio_handler_backends.CancelUpdate` are ignored: they mean
  "this update was intentionally dropped" (maintenance / access / throttle), not
  "something broke";
* :func:`run_polling` — wraps ``bot.polling()`` in :func:`app.app.run`, so a
  failure of the long-poll loop itself is reported the same way before being
  re-raised for the process supervisor.

Both paths log the full traceback (the :class:`~app.logging_setup.SecretMaskingFilter`
masks secrets in it) and alert owners **at most once per distinct error
signature per window**. The limiter itself is
:meth:`app.services.notifier.Notifier.allow_error_alert` (M0-07.4): there is
exactly one implementation, and this module only consumes it.
"""

from __future__ import annotations

import logging
import os
from collections.abc import Iterable
from types import TracebackType
from typing import Any

from telebot.asyncio_handler_backends import BaseMiddleware, CancelUpdate

from app.logging_setup import mask_secrets, normalize_secrets
from app.services.notifier import Notifier
from app.utils.text import esc

logger = logging.getLogger(__name__)

#: Longest rendered exception text quoted back in the owner alert.
MAX_EXC_CHARS = 500
#: Package whose frames count as "application code" for signature purposes.
APP_PACKAGE = "app"
#: Path fragments that mark a file as belonging to the application package.
_APP_PATH_MARKERS = (f"{os.sep}{APP_PACKAGE}{os.sep}", f"/{APP_PACKAGE}/", "\\app\\")


def _is_app_frame(frame: Any) -> bool:
    """Return ``True`` when ``frame`` belongs to the application package."""
    module = str(frame.f_globals.get("__name__") or "")
    if module == APP_PACKAGE or module.startswith(f"{APP_PACKAGE}."):
        return True
    filename = frame.f_code.co_filename
    return any(marker in filename for marker in _APP_PATH_MARKERS)


def error_signature(exc: BaseException) -> str:
    """Return a stable ``Type:module:function:line`` signature for ``exc``.

    The exception *type* is part of the signature, and the location prefers the
    **innermost application frame**. A failure raised inside a shared library
    frame (``json.loads``, SQLAlchemy's ``execute``, ``httpx``…) would otherwise
    give two unrelated bugs the same signature, and the 10-minute limiter in
    :meth:`Notifier.allow_error_alert` would silently swallow the second alert.
    The absolute innermost frame is used only when no application frame exists
    (e.g. ``pydantic.ValidationError``, which is re-raised from library code and
    carries no application frame at all).
    """
    innermost: TracebackType | None = None
    app_frame: TracebackType | None = None
    tb = exc.__traceback__
    while tb is not None:
        innermost = tb
        if _is_app_frame(tb.tb_frame):
            app_frame = tb
        tb = tb.tb_next
    chosen = app_frame or innermost
    exc_type = type(exc).__name__
    if chosen is None:
        return f"{exc_type}:?:?:?"
    frame = chosen.tb_frame
    module = str(frame.f_globals.get("__name__") or "?")
    return f"{exc_type}:{module}:{frame.f_code.co_name}:{chosen.tb_lineno}"


class ErrorReporter:
    """Report unhandled failures: one log record, at most one owner alert."""

    def __init__(
        self,
        notifier: Notifier | None = None,
        *,
        secrets: Iterable[str] = (),
    ) -> None:
        self._notifier = notifier
        # An exception message can quote a token; alerts are user-visible, so
        # run them through the same masking rule as the logs (§0.1 rule 6).
        self._secrets = normalize_secrets(secrets)

    def attach_notifier(self, notifier: Notifier) -> None:
        """Wire the notifier at startup (mirrors ``Notifier.attach_bot``)."""
        self._notifier = notifier

    async def report(self, exc: BaseException, *, context: str = "handler") -> bool:
        """Log ``exc`` with its traceback and alert owners when not suppressed.

        Returns ``True`` when an owner alert was actually sent. Never raises.
        """
        signature = error_signature(exc)
        logger.error(
            "unhandled %s error [%s]: %s", context, signature, exc, exc_info=exc
        )
        notifier = self._notifier
        if notifier is None:
            return False
        if not notifier.allow_error_alert(signature):
            logger.info("suppressing repeat alert for %s", signature)
            return False
        try:
            await notifier.alert_staff(
                self.format_alert(exc, context=context, signature=signature),
                kind="alert",
                parse_mode="HTML",
            )
        except Exception:  # pragma: no cover - alerting must never raise
            logger.warning("failed to send error alert", exc_info=True)
            return False
        return True

    def format_alert(self, exc: BaseException, *, context: str, signature: str) -> str:
        """Build the HTML-safe owner alert for ``exc`` (summary, not traceback)."""
        detail = f"{type(exc).__name__}: {exc}"[:MAX_EXC_CHARS]
        return (
            "⚠️ <b>Ошибка в боте</b>\n"
            f"Контекст: <code>{esc(context)}</code>\n"
            f"<code>{esc(mask_secrets(detail, self._secrets))}</code>\n"
            f"Подпись: <code>{esc(signature)}</code>"
        )


class ErrorReporterMiddleware(BaseMiddleware):
    """Report exceptions raised inside handlers (telebot ``post_process``)."""

    def __init__(self, reporter: ErrorReporter | None = None) -> None:
        self._reporter = reporter or ErrorReporter()
        self.update_types = ["message", "callback_query"]

    async def pre_process(self, update: Any, data: dict[str, Any]) -> Any:
        """No gating: every update runs normally."""
        return None

    async def post_process(
        self, update: Any, data: dict[str, Any], exception: Any
    ) -> None:
        """Report the handler exception, if the handler raised one.

        Telebot passes an ``Exception`` instance here... but it also hands over
        its own control-flow sentinel :class:`CancelUpdate` (returned by the
        maintenance / access / throttle gates), and that sentinel is **not** a
        ``BaseException`` subclass in this telebot version. A dropped update is
        an expected outcome, not a bug: reporting it would log an error and wake
        the owners on every blocked user. The annotation is therefore ``Any``.
        """
        if exception is None or isinstance(exception, CancelUpdate):
            return
        try:
            await self._reporter.report(exception, context="handler")
        except Exception:  # pragma: no cover - reporting must never propagate
            logger.warning("error reporter failed", exc_info=True)


async def run_polling(bot: Any, reporter: ErrorReporter, **kwargs: Any) -> None:
    """Run ``bot.polling()`` and report a failure before re-raising it."""
    try:
        await bot.polling(**kwargs)
    except Exception as exc:
        await reporter.report(exc, context="polling")
        raise

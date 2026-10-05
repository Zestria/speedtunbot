"""``/profile`` — subscription overview (``TASK_PLAN.md`` §M0-09.2, fixes B4).

Reads the client through :class:`~app.services.panel.PanelGateway` (never through
``py3xui`` directly) and always replies: a panel outage yields a friendly message
plus a staff alert, and a user without an account is told to run ``/start``.

B4 was an ``UnboundLocalError``: the legacy handler caught the panel failure but
then used the never-assigned ``inbound`` variable. Here a failed read returns
immediately.
"""

from __future__ import annotations

import logging
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo

from app import texts
from app.container import Container
from app.errors import PanelError
from app.handlers.common import alert_staff, reply
from app.utils.text import esc

logger = logging.getLogger(__name__)


def profile_text(client: Any, *, sub_url_base: str, timezone: str) -> str:
    """Render the profile card for a panel ``client`` (``expiry 0`` = perpetual)."""
    status = (
        texts.PROFILE_STATUS_ACTIVE if client.enable else texts.PROFILE_STATUS_INACTIVE
    )
    expiry_ms = int(client.expiry_time or 0)
    # ``expiry_time == 0`` is the panel's "unlimited" marker (§0.2 / B4).
    if expiry_ms == 0:
        expires = texts.PROFILE_UNLIMITED
    else:
        expires = datetime.fromtimestamp(
            expiry_ms / 1000, tz=ZoneInfo(timezone)
        ).strftime("%d.%m.%Y %H:%M")
    link = f"{sub_url_base}{client.sub_id}"
    return (
        f"{texts.PROFILE_TITLE}\n\n"
        f"{texts.PROFILE_STATUS_LABEL} {status}\n"
        f"{texts.PROFILE_EXPIRES_LABEL} {expires}\n\n"
        f"{texts.PROFILE_LINK_HEADING}\n"
        f"<code>{esc(link)}</code>\n\n"
        f"{texts.PROFILE_LINK_HINT}"
    )


async def profile_command(message: Any, bot: Any, container: Container) -> None:
    """Show the caller's subscription; always reply, never raise on a panel error."""
    tg_id = int(message.from_user.id)
    chat_id = int(message.chat.id)

    panel = container.panel
    if panel is None:
        await reply(bot, chat_id, texts.ERROR_GENERIC)
        return

    try:
        client = await panel.get_client(tg_id)
    except PanelError as exc:
        # B4: no ``inbound`` variable to touch here — we simply stop.
        logger.warning("panel failure in /profile for %s: %s", tg_id, exc)
        await reply(bot, chat_id, texts.ERROR_PANEL)
        await alert_staff(container, "profile", exc)
        return

    if client is None:
        await reply(bot, chat_id, texts.PROFILE_NO_ACCOUNT)
        return

    settings = container.settings
    await reply(
        bot,
        chat_id,
        profile_text(
            client, sub_url_base=settings.sub_url_base, timezone=settings.timezone
        ),
        parse_mode="HTML",
    )


def register_profile_handler(bot: Any, container: Container) -> None:
    """Register ``/profile`` on ``bot``."""

    @bot.message_handler(commands=["profile"])
    async def _profile(message: Any) -> None:  # pragma: no cover - thin adapter
        await profile_command(message, bot, container)

"""QR-code rendering for the ``📱 QR-код`` screen (``TASK_PLAN.md`` §S1-2).

The PNG is produced **in memory**: ``send_photo`` accepts a file-like object, so
the bot never writes a temporary file (a container filesystem may be read-only,
and a leftover ``/tmp`` QR would leak the subscription URL to the host).

Rendering is deterministic — the same URL always yields the same bytes — which
is what §S1-2.1 asserts and what keeps the screen cache-friendly.
"""

from __future__ import annotations

from io import BytesIO

import segno

#: §S1-2.1: 10 px per module and a 3-module quiet zone scan reliably from a
#: phone screen; the indigo modules keep the contrast well above the ~40 %
#: reflectivity delta scanners need while matching the bot's palette.
QR_SCALE = 10
QR_BORDER = 3
QR_DARK = "#1a237e"
QR_LIGHT = "#ffffff"


def make_qr_png(url: str) -> BytesIO:
    """Render ``url`` into a rewound, named in-memory PNG (§S1-2.1).

    The buffer is seeked back to ``0`` and given a ``.name`` so it can be handed
    to ``send_photo`` as an upload: telebot uploads a file-like object as-is and
    falls back to that name for the multipart filename.
    """
    buffer = BytesIO()
    segno.make(url).save(
        buffer,
        kind="png",
        scale=QR_SCALE,
        border=QR_BORDER,
        dark=QR_DARK,
        light=QR_LIGHT,
    )
    buffer.seek(0)
    buffer.name = "qr.png"
    return buffer


__all__ = ["QR_BORDER", "QR_DARK", "QR_LIGHT", "QR_SCALE", "make_qr_png"]

"""QR rendering (``TASK_PLAN.md`` §S1-2.1).

The buffer is handed straight to ``send_photo``, so it has to be a real PNG, it
has to sit at offset 0, and the same URL has to render the same bytes — the
whole AC of §S1-2.1.
"""

from __future__ import annotations

from app.utils.qr import make_qr_png

#: First 8 bytes of every PNG file.
PNG_SIGNATURE = b"\x89PNG\r\n\x1a\n"
URL = "https://panel.example.com/sub/abc123"


def test_make_qr_png_returns_a_rewound_named_buffer() -> None:
    """AC (S1-2.1): the buffer is named ``qr.png`` and positioned at ``0``."""
    buffer = make_qr_png(URL)

    assert buffer.name == "qr.png"
    assert buffer.tell() == 0


def test_make_qr_png_writes_a_png() -> None:
    """AC (S1-2.1): the payload starts with the PNG signature."""
    assert make_qr_png(URL).read().startswith(PNG_SIGNATURE)


def test_make_qr_png_is_deterministic() -> None:
    """AC (S1-2.1): the same URL always renders the same bytes."""
    assert make_qr_png(URL).read() == make_qr_png(URL).read()


def test_make_qr_png_encodes_the_url_itself() -> None:
    """A different subscription is a different code — the URL is the payload."""
    assert make_qr_png(URL).read() != make_qr_png(URL + "x").read()

"""Text helpers (``TASK_PLAN.md`` §S1-1.6, defects B9).

``sub_url`` is the single place a subscription link is built, and ``esc`` is the
HTML guard every user-controlled fragment passes through.
"""

from __future__ import annotations

from app.settings import Settings
from app.utils.text import esc, sub_url


def test_sub_url_joins_the_base_and_the_id(settings: Settings) -> None:
    """AC (S1-1.6): ``sub_url(settings, sub_id)`` is base + id."""
    assert sub_url(settings, "abc123") == "https://panel.example.com/sub/abc123"


def test_sub_url_follows_the_configured_base(settings: Settings) -> None:
    custom = settings.model_copy(update={"sub_url_base": "https://other.example/"})

    assert sub_url(custom, "xyz") == "https://other.example/xyz"


def test_esc_escapes_the_html_metacharacters() -> None:
    assert esc("a & b < c > d") == "a &amp; b &lt; c &gt; d"

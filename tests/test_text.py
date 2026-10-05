"""Text helpers (``TASK_PLAN.md`` §S1-1.6, defects B9).

``sub_url`` is the single place a subscription link is built, and ``esc`` is the
HTML guard every user-controlled fragment passes through.
"""

from __future__ import annotations

from app import texts
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


# --- instruction catalogue (S1-3.1) -----------------------------------------


def test_instruction_platforms_agree_across_the_catalogue() -> None:
    """AC (S1-3.1): the four platforms line up in keys, labels and order."""
    assert texts.INSTRUCTION_PLATFORMS == ("android", "ios", "windows", "macos")
    assert set(texts.INSTRUCTIONS) == set(texts.INSTRUCTION_PLATFORMS)
    assert set(texts.INSTR_PLATFORM_LABELS) == set(texts.INSTRUCTION_PLATFORMS)


def test_every_instruction_template_is_filled_from_the_users_link() -> None:
    """AC (S1-3.1, S1-3.5): a template, never a hard-coded URL."""
    for platform, template in texts.INSTRUCTIONS.items():
        steps = [line for line in template.splitlines() if line[:1].isdigit()]
        assert "{link}" in template, platform
        assert "http" not in template, platform
        assert 3 <= len(steps) <= 5, platform


# --- main menu labels (S1-4.4) ----------------------------------------------


def test_menu_labels_are_distinct_and_shared_with_the_profile_card() -> None:
    """AC (S1-4.4): four distinct labels, two of them the dashboard's own."""
    labels = (
        texts.BUTTON_MENU_PROFILE,
        texts.BUTTON_MENU_PAY,
        texts.BUTTON_MENU_INSTR,
        texts.BUTTON_MENU_SUPPORT,
    )
    assert len(set(labels)) == 4
    # Shared by identity, so the menu and the card cannot drift apart.
    assert texts.BUTTON_MENU_INSTR is texts.BUTTON_PROFILE_INSTR
    assert texts.BUTTON_MENU_SUPPORT is texts.BUTTON_PROFILE_SUPPORT


def test_menu_texts_point_at_the_menu_itself_and_the_commands() -> None:
    """AC (S1-4.4): the menu card and /help are plain text, no link expected."""
    assert texts.START_MENU
    for name in ("/start", "/profile", "/pay", "/support"):
        assert name in texts.HELP_TEXT


# --- link regeneration + post-payment copy (S1-5) ---------------------------


def test_newlink_copy_is_plain_text_without_placeholders() -> None:
    """AC (S1-5.2): the confirmation and failure cards need no substitution."""
    assert "Старая ссылка перестанет работать" in texts.NEWLINK_CONFIRM
    assert "{link}" not in texts.NEWLINK_CONFIRM
    assert "{link}" not in texts.NEWLINK_FAILED
    assert "10" in texts.NEWLINK_RATE_LIMITED


def test_payment_instructions_bold_the_price_and_code_the_details() -> None:
    """AC (S1-5.5): the amount is bold, the account is a ``<code>`` block."""
    assert "<b>{price} ₽</b>" in texts.PAYMENT_INSTRUCTIONS
    assert "<code>{bank_details}</code>" in texts.PAYMENT_INSTRUCTIONS


def test_post_payment_copy_and_buttons_match_the_menu() -> None:
    """AC (S1-5.6): the finite copy takes an expiry, the unlimited one does not."""
    assert "{expiry}" in texts.PAYMENT_USER_APPROVED
    assert "{expiry}" not in texts.PAYMENT_USER_APPROVED_UNLIMITED
    assert texts.PAYMENT_USER_APPROVED_UNLIMITED
    # Shared by identity, so the notification cannot name the screens differently.
    assert texts.BUTTON_GO_PROFILE is texts.BUTTON_MENU_PROFILE
    assert texts.BUTTON_GO_SUPPORT is texts.BUTTON_MENU_SUPPORT

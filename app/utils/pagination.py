"""List pagination helpers (``TASK_PLAN.md`` §2.6 / M0-07.8).

Pure functions shared by every paginated card (payments, users, invites, …).
The page size is fixed at :data:`PAGE_SIZE` (8) as required by the plan; the
:func:`nav_row` builder returns the ``prev``/``next`` inline-keyboard row using
caller-supplied ``callback_data`` so callers keep owning their namespaces.
"""

from __future__ import annotations

from dataclasses import dataclass

PAGE_SIZE = 8


@dataclass(frozen=True)
class Page:
    """One slice of a list plus the index bookkeeping the buttons need."""

    items: list
    index: int
    count: int

    @property
    def has_prev(self) -> bool:
        return self.index > 0

    @property
    def has_next(self) -> bool:
        return self.index + 1 < self.count


def page_count(total: int, page_size: int = PAGE_SIZE) -> int:
    """Return the number of pages for ``total`` items (always ``>= 1``)."""
    if page_size <= 0:
        raise ValueError("page_size must be positive")
    if total <= 0:
        return 1
    return (total + page_size - 1) // page_size


def clamp_page(page: int, total: int, page_size: int = PAGE_SIZE) -> int:
    """Clamp ``page`` into ``[0, page_count(total) - 1]``."""
    count = page_count(total, page_size)
    return max(0, min(int(page), count - 1))


def paginate(items: list, page: int, page_size: int = PAGE_SIZE) -> Page:
    """Slice ``items`` for ``page`` (0-based) and return a :class:`Page`."""
    total = len(items)
    count = page_count(total, page_size)
    index = max(0, min(int(page), count - 1))
    start = index * page_size
    return Page(items=list(items[start : start + page_size]), index=index, count=count)


def nav_row(
    page: Page,
    *,
    prev_data: str,
    next_data: str,
    prev_label: str = "⬅️ Назад",
    next_label: str = "Вперёд ➡️",
) -> list[tuple[str, str]]:
    """Return the ``prev``/``next`` buttons for ``page`` (empty when single page).

    Buttons are plain ``(label, callback_data)`` tuples so this helper stays free
    of any telebot dependency; handlers assemble them into an inline keyboard.
    """
    row: list[tuple[str, str]] = []
    if page.has_prev:
        row.append((prev_label, prev_data))
    if page.has_next:
        row.append((next_label, next_data))
    return row

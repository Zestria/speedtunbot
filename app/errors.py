"""Domain exceptions used across the service layer.

Handlers catch these by type and answer the user with a friendly Russian
message (see TASK_PLAN.md §0.1 rule 7). Keep this module free of any I/O
imports so it can be imported from anywhere without side effects.
"""

from __future__ import annotations


class AppError(Exception):
    """Base class for every domain error raised by the application."""


class PanelError(AppError):
    """Base class for errors coming from the 3x-ui panel."""


class PanelUnavailable(PanelError):
    """The panel could not be reached or answered with a transport error."""


class ClientNotFound(PanelError):
    """The requested panel client does not exist."""


class NotSupportedError(PanelError):
    """The panel/py3xui cannot do this (e.g. Xray restart)."""


class PermissionDenied(AppError):
    """The acting user lacks the permission required for the action."""


class InvalidCallback(AppError):
    """Callback data is malformed, unknown, or fails validation."""


class AlreadyProcessed(AppError):
    """The entity was already handled (idempotency guard)."""

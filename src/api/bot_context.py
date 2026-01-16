"""Shared bot application context to avoid circular imports."""

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from telegram.ext import Application

# Global bot application reference (set via setup_webhook_route)
_bot_app: "Application | None" = None  # type: ignore[type-arg]


def get_bot_app() -> "Application | None":  # type: ignore[type-arg]
    """Get the global bot application instance.

    Returns:
        The bot Application instance or None if not initialized.
    """
    return _bot_app


def set_bot_app(app: "Application") -> None:  # type: ignore[type-arg]
    """Set the global bot application instance.

    Args:
        app: The bot Application instance.
    """
    global _bot_app
    _bot_app = app


__all__ = ["get_bot_app", "set_bot_app"]

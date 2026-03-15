"""Shared error handling utilities for bot handlers."""

import asyncio
import logging
from typing import Optional

import httpx
from telegram import InlineKeyboardMarkup, Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import ContextTypes

from src.services.localizer import t

logger = logging.getLogger(__name__)


async def handle_conversation_error(
    e: Exception,
    update: Update,
    error_context: str,
    cleanup_fn: Optional[callable] = None,
    context: Optional[ContextTypes.DEFAULT_TYPE] = None,
) -> int:
    """Handle errors in conversation handlers with network-specific messages.

    Args:
        e: The exception that was caught
        update: The Update object (may have message or callback_query)
        error_context: Description of where error occurred (for logging)
        cleanup_fn: Optional cleanup function to call (e.g., _clear_payout_context)
        context: Optional context to pass to cleanup_fn

    Returns:
        States.END constant (-1)
    """
    # Determine error type and message
    is_network_error = isinstance(
        e,
        (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, TimedOut, NetworkError),
    )
    error_type = "Network error" if is_network_error else "Error"
    error_msg = t("err_network_retry") if is_network_error else t("err_processing")

    # Log the error
    logger.error(f"{error_type} {error_context}: %s", e, exc_info=True)

    # Notify user (with nested try-except to handle secondary failures)
    try:
        if update.callback_query:
            await update.callback_query.edit_message_text(
                error_msg, reply_markup=InlineKeyboardMarkup([])
            )
        elif update.message:
            await update.message.reply_text(error_msg)
    except Exception:
        logger.debug("Could not send error notification to user", exc_info=True)

    # Call cleanup function if provided
    if cleanup_fn and context:
        cleanup_fn(context)

    return -1  # States.END

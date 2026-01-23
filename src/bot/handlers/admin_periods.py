"""Service period management handlers with conversation state machine."""

import asyncio
import logging
from datetime import date

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.ext import ContextTypes

from src.bot.handlers.error_utils import handle_conversation_error
from src.services import AsyncSessionLocal, ServicePeriodService
from src.services.auth_service import verify_bot_admin_authorization
from src.services.localizer import t
from src.utils.parsers import parse_date

# Backward-compatible alias for tests and existing patch paths
verify_admin_authorization = verify_bot_admin_authorization

logger = logging.getLogger(__name__)


# Conversation state constants
class States:
    """Conversation states for service periods workflow."""

    END = -1
    SELECT_ACTION = 10
    INPUT_START_DATE = 11
    INPUT_PERIOD_MONTHS = 12


# Context data keys
_PERIODS_KEYS = [
    "periods_admin_id",
    "period_start_date",
    "period_months",
    "period_id",
    "period_name",
    "authorized_admin",
]


def _clear_periods_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all periods-related context data."""
    for key in _PERIODS_KEYS:
        context.user_data.pop(key, None)


def _validate_date(text: str) -> tuple[date | None, bool]:
    """Validate and parse a date in DD.MM.YYYY format.

    Args:
        text: Input text to parse

    Returns:
        Tuple of (parsed_date, is_valid). If invalid, date is None.
    """
    try:
        value = parse_date(text)
        if value is None:
            return None, False
        return value, True
    except ValueError:
        return None, False


def _build_previous_value_keyboard(previous_value: str | None) -> ReplyKeyboardMarkup | None:
    """Build a keyboard with a previous value button if available."""
    if not previous_value:
        return None
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text=previous_value)]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )


async def handle_periods_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel/reset service periods workflow.

    Clears all conversation context and ends the conversation.
    Called when user types /periods while already in active conversation.
    """
    _clear_periods_context(context)
    return States.END


async def handle_periods_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start service periods management workflow.

    Admin command to create and manage service periods.
    Entry point for period management conversation.

    Returns:
        Conversation state for next step (SELECT_ACTION)
    """
    try:
        if not update.message or not update.message.from_user:
            logger.warning("Received periods command without message or user")
            return States.END

        telegram_id = update.message.from_user.id

        # Verify admin authorization
        admin_user = await verify_admin_authorization(telegram_id)
        if not admin_user:
            logger.warning("Non-admin attempted periods command: telegram_id=%d", telegram_id)
            try:
                await update.message.reply_text(t("err_not_authorized"))
            except Exception:
                pass
            return States.END

        # Store authenticated admin user context
        context.user_data["authorized_admin"] = admin_user
        context.user_data["periods_admin_id"] = telegram_id

        # Show action menu
        buttons = [
            [InlineKeyboardButton(t("btn_new_period"), callback_data="period_action:create")],
            [InlineKeyboardButton(t("btn_view_periods"), callback_data="period_action:view")],
            [InlineKeyboardButton(t("btn_close_period"), callback_data="period_action:close")],
        ]
        keyboard = InlineKeyboardMarkup(buttons)

        await update.message.reply_text(
            t("prompt_select_action"),
            reply_markup=keyboard,
        )

        logger.info(
            "Service periods workflow started by admin user_id=%d (telegram_id=%d)",
            admin_user.id,
            telegram_id,
        )

        return States.SELECT_ACTION

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "starting periods workflow")


async def handle_period_action_selection(  # noqa: C901
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle period action selection: create new period, view existing periods, or close period."""
    try:
        cq = update.callback_query
        if not cq or not cq.data:
            logger.warning("Received period action callback without data")
            return States.END

        await cq.answer()

        if cq.data == "period_action:create":
            # Start new period creation flow
            # Query max end_date from existing periods to suggest as default start date
            async with AsyncSessionLocal() as session:
                period_service = ServicePeriodService(session)
                last_period = await period_service.get_latest_period()

                keyboard = None
                if last_period and last_period.end_date:
                    suggested_start_str = last_period.end_date.strftime("%d.%m.%Y")
                    keyboard = _build_previous_value_keyboard(suggested_start_str)

                await cq.message.reply_text(t("prompt_period_start_date"), reply_markup=keyboard)
                return States.INPUT_START_DATE

        elif cq.data == "period_action:view":
            # Show existing periods
            async with AsyncSessionLocal() as session:
                period_service = ServicePeriodService(session)
                periods = await period_service.list_periods(limit=10)

                if not periods:
                    await cq.edit_message_text(t("empty_periods"))
                    return States.END

                periods_text = t("title_existing_periods") + "\n\n"
                for period in periods:
                    status_emoji = "🟢" if period.status == "open" else "🔴"
                    status_text = (
                        t("status_open") if period.status == "open" else t("status_closed")
                    )
                    periods_text += (
                        f"{status_emoji} {period.name}\n   {t('title_status')} {status_text}\n\n"
                    )

                await cq.edit_message_text(periods_text, parse_mode="Markdown")
                return States.END

        elif cq.data == "period_action:close":
            # Show list of open periods to close
            async with AsyncSessionLocal() as session:
                period_service = ServicePeriodService(session)
                open_periods = await period_service.get_open_periods()

                if not open_periods:
                    await cq.edit_message_text(t("empty_periods_to_close"))
                    return States.END

                # Build buttons for each open period
                buttons = []
                for period in open_periods:
                    buttons.append(
                        [
                            InlineKeyboardButton(
                                f"🟢 {period.name}", callback_data=f"close_period:{period.id}"
                            )
                        ]
                    )
                keyboard = InlineKeyboardMarkup(buttons)

                await cq.edit_message_text(
                    t("prompt_select_period_to_close"),
                    reply_markup=keyboard,
                )
                return States.SELECT_ACTION

        else:
            logger.warning("Unknown period action: %s", cq.data)
            await cq.edit_message_text(t("err_processing"))
            return States.END

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "in period action selection")


async def handle_period_start_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle start date input for new service period.

    Validates date format (DD.MM.YYYY) and that date is first day of month.
    """
    try:
        if not update.message or not update.message.text:
            return States.INPUT_START_DATE

        text = update.message.text.strip()
        value, valid = _validate_date(text)

        if not valid:
            await update.message.reply_text(t("err_invalid_date_format"))
            return States.INPUT_START_DATE

        # Validate that date is first day of month
        if value.day != 1:
            await update.message.reply_text(
                t("err_invalid_date_format") + "\n" + t("hint_start_date_first_of_month")
            )
            return States.INPUT_START_DATE

        context.user_data["period_start_date"] = value

        await update.message.reply_text(t("prompt_period_months"))
        return States.INPUT_PERIOD_MONTHS

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "in period start date input")


async def handle_period_months_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle period months input for new service period.

    Validates that input is an integer between 1 and 12.
    Creates ServicePeriod with specified period_months duration.
    Status is set to "open" by design.
    """
    try:
        if not update.message or not update.message.text:
            return States.INPUT_PERIOD_MONTHS

        text = update.message.text.strip()

        # Parse and validate period_months
        try:
            period_months = int(text)
        except ValueError:
            await update.message.reply_text(t("err_invalid_period_months"))
            return States.INPUT_PERIOD_MONTHS

        if not (1 <= period_months <= 12):
            await update.message.reply_text(t("err_period_months_range"))
            return States.INPUT_PERIOD_MONTHS

        start_date = context.user_data.get("period_start_date")
        if not start_date:
            logger.warning("Missing period_start_date in context")
            await update.message.reply_text(t("err_processing"))
            return States.END

        context.user_data["period_months"] = period_months

        # Create ServicePeriod via service
        async with AsyncSessionLocal() as session:
            period_service = ServicePeriodService(session)
            admin_id = context.user_data.get("periods_admin_id")
            new_period = await period_service.create_period(
                start_date,
                period_months=period_months,
                actor_id=admin_id,
            )

            context.user_data["period_id"] = new_period.id
            context.user_data["period_name"] = new_period.name

            logger.info(
                "Created new service period: id=%d, name=%s, dates=%s to %s, period_months=%d, admin_id=%d",
                new_period.id,
                new_period.name,
                start_date,
                new_period.end_date,
                period_months,
                context.user_data.get("periods_admin_id"),
            )

            # Show confirmation with period details
            status_emoji = "🟢"
            message = (
                f"✅ {t('msg_period_created')}\n\n"
                f"{status_emoji} {new_period.name}\n"
                f"   {t('title_status')} {t('status_open')}"
            )
            await update.message.reply_text(message)

            return States.END

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "in period months input")


async def handle_close_period_confirmation(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle period closure confirmation.

    Close selected period without generating any bills.
    """
    try:
        cq = update.callback_query
        if not cq or not cq.data:
            return States.END

        await cq.answer()

        # Extract period ID
        try:
            period_id = int(cq.data.split(":")[1])
        except (IndexError, ValueError):
            logger.warning("Invalid close period callback data: %s", cq.data)
            await cq.edit_message_text(t("err_processing"))
            return States.END

        async with AsyncSessionLocal() as session:
            period_service = ServicePeriodService(session)
            period = await period_service.get_by_id(period_id)

            if not period:
                await cq.edit_message_text(t("err_processing"))
                return States.END

            admin_user = context.user_data.get("authorized_admin")
            actor_id = admin_user.id if admin_user else None

            # Close the period using service method
            success = await period_service.close_period(
                period_id=period_id,
                actor_id=actor_id,
            )

            if not success:
                await cq.edit_message_text(t("err_processing"))
                return States.END

            await cq.edit_message_text(t("msg_period_closed", period_name=period.name))

            # Clear any context
            _clear_periods_context(context)

            return States.END

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(
            e, update, "closing period", cleanup_fn=_clear_periods_context, context=context
        )


__all__ = [
    "States",
    "handle_periods_command",
    "handle_periods_cancel",
    "handle_period_action_selection",
    "handle_period_start_date_input",
    "handle_period_months_input",
    "handle_close_period_confirmation",
]

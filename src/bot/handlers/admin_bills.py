"""Electricity bills management handlers with conversation state machine."""

import asyncio
import logging
from decimal import Decimal, InvalidOperation

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
from src.models.service_period import ServicePeriod
from src.models.user import User
from src.services import AsyncSessionLocal, BillsService, ServicePeriodService
from src.services.auth_service import verify_bot_admin_authorization
from src.services.bills_service import OwnerShare, PersonalElectricityBill
from src.services.locale_service import format_currency
from src.services.localizer import t
from src.utils.parsers import parse_russian_decimal

# Backward-compatible alias for tests and existing patch paths
verify_admin_authorization = verify_bot_admin_authorization

logger = logging.getLogger(__name__)


# Conversation state constants
class States:
    """Conversation states for bills workflow (readings/budget/close)."""

    END = -1
    SELECT_PERIOD = 1
    SELECT_ACTION = 2
    # Electricity (readings) states
    INPUT_METER_START = 3
    INPUT_METER_END = 4
    INPUT_MULTIPLIER = 5
    INPUT_RATE = 6
    INPUT_LOSSES = 7
    CONFIRM_ELECTRICITY_BILLS = 8
    # Budget states
    INPUT_MAIN_BUDGET = 9
    INPUT_CONSERVATION_BUDGET = 10
    CONFIRM_BUDGET_BILLS = 11


# Context data keys
_SHARED_BILLS_KEYS = [
    "bills_admin_id",
    "bills_period_id",
    "bills_period_name",
    "authorized_admin",
]

_ELECTRICITY_KEYS = [
    "electricity_admin_id",
    "electricity_period_id",
    "electricity_period_name",
    "electricity_start",
    "electricity_end",
    "electricity_multiplier",
    "electricity_rate",
    "electricity_losses",
    "electricity_total_cost",
    "electricity_personal_bills",
    "electricity_personal_bills_sum",
    "electricity_shared_cost",
    "electricity_owner_shares",
    "electricity_previous_multiplier",
    "electricity_previous_rate",
    "electricity_previous_losses",
    "authorized_admin",
]


def _clear_electricity_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all electricity-related context data."""
    for key in _ELECTRICITY_KEYS:
        context.user_data.pop(key, None)


_BUDGET_KEYS = [
    "budget_admin_id",
    "budget_period_id",
    "budget_period_name",
    "budget_year_budget",
    "budget_conservation_year_budget",
    "budget_main_calculations",
    "budget_conservation_calculations",
    "budget_previous_year_budget",
    "budget_previous_conservation_year_budget",
    "authorized_admin",
]


def _clear_budget_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all budget-related context data."""
    for key in _BUDGET_KEYS:
        context.user_data.pop(key, None)


def _validate_positive_decimal(text: str, allow_zero: bool = False) -> tuple[Decimal | None, bool]:
    """Validate and parse a positive decimal number.

    Args:
        text: Input text to parse
        allow_zero: If True, zero is valid; if False, must be > 0

    Returns:
        Tuple of (parsed_value, is_valid). If invalid, value is None.
    """
    try:
        value = parse_russian_decimal(text)
        if value is None:
            return None, False
        if allow_zero:
            if value < 0:
                return None, False
        else:
            if value <= 0:
                return None, False
        return value, True
    except (InvalidOperation, ValueError):
        return None, False


def _validate_fraction(text: str) -> tuple[Decimal | None, bool]:
    """Validate and parse a decimal between 0 and 1 (inclusive).

    Args:
        text: Input text to parse

    Returns:
        Tuple of (parsed_value, is_valid). If invalid, value is None.
    """
    try:
        value = parse_russian_decimal(text)
        if value is None or value < 0 or value > 1:
            return None, False
        return value, True
    except (InvalidOperation, ValueError):
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


async def handle_bills_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel/reset bills workflow.

    Clears all conversation context and ends the conversation.
    Called when user types /bills while already in active conversation.
    """
    _clear_electricity_context(context)
    _clear_budget_context(context)
    # Clear shared bills context
    for key in _SHARED_BILLS_KEYS:
        context.user_data.pop(key, None)
    return States.END


async def handle_bills_command(  # noqa: C901
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Start bills management workflow.

    Admin command to manage bills - choose period, then select action:
    - Generate bills based on readings (electricity)
    - Generate bills based on budget (MAIN + CONSERVATION)
    - Close period without generating bills

    Entry point for multi-step conversation.
    Usage: /bills

    Returns:
        Conversation state for next step (SELECT_PERIOD)
    """
    try:
        if not update.message or not update.message.from_user:
            logger.warning("Received electricity command without message or user")
            return States.END

        telegram_id = update.message.from_user.id

        # Verify admin authorization
        admin_user = await verify_admin_authorization(telegram_id)
        if not admin_user:
            logger.warning("Non-admin attempted electricity command: telegram_id=%d", telegram_id)
            try:
                await update.message.reply_text(t("err_not_authorized"))
            except Exception:
                pass
            return States.END

        async with AsyncSessionLocal() as session:
            # Query open service periods
            period_service = ServicePeriodService(session)
            open_periods = await period_service.get_open_periods()

            # Build inline buttons for period selection
            buttons = []
            for period in open_periods:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"📅 {period.name}", callback_data=f"bill_period:{period.id}"
                        )
                    ]
                )

            keyboard = InlineKeyboardMarkup(buttons)

            await update.message.reply_text(
                t("prompt_select_period"),
                reply_markup=keyboard,
            )

            logger.info(
                "Bills workflow started by admin user_id=%d (telegram_id=%d)",
                admin_user.id,
                telegram_id,
            )

            # Store authenticated admin user context
            context.user_data["authorized_admin"] = admin_user
            context.user_data["bills_admin_id"] = telegram_id

            return States.SELECT_PERIOD

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "starting bills workflow")


async def handle_period_selection(  # noqa: C901
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle service period selection and show action options.

    User selects existing open service period, then chooses:
    - Generate bills based on readings (electricity)
    - Generate bills based on budget (MAIN + CONSERVATION)
    - Close period without generating bills
    """
    try:
        cq = update.callback_query
        if not cq or not cq.data:
            logger.warning("Received period selection callback without data")
            return States.END

        await cq.answer()

        async with AsyncSessionLocal() as session:
            period_service = ServicePeriodService(session)

            # Extract period ID
            try:
                period_id = int(cq.data.split(":")[1])
            except (IndexError, ValueError):
                logger.warning("Invalid period callback data: %s", cq.data)
                await cq.edit_message_text(t("err_processing"))
                return States.END

            # Fetch period
            period = await period_service.get_by_id(period_id)
            if not period:
                logger.warning("Period %d not found", period_id)
                await cq.edit_message_text(t("err_processing"))
                return States.END

            # Store selected period (in both contexts for compatibility)
            context.user_data["bills_period_id"] = period_id
            context.user_data["bills_period_name"] = period.name

            # Show 2 action buttons
            buttons = [
                [
                    InlineKeyboardButton(
                        t("btn_create_by_readings"),
                        callback_data=f"bill_action:readings:{period_id}",
                    )
                ],
                [
                    InlineKeyboardButton(
                        t("btn_create_by_budget"),
                        callback_data=f"bill_action:budget:{period_id}",
                    )
                ],
            ]
            keyboard = InlineKeyboardMarkup(buttons)

            await cq.edit_message_text(
                t("msg_bills_action", period_name=period.name),
                reply_markup=keyboard,
            )

            return States.SELECT_ACTION

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "in period selection")


async def handle_action_selection(  # noqa: C901
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle action selection: readings, budget, or close period."""
    try:
        cq = update.callback_query
        if not cq or not cq.data:
            logger.warning("Received action selection callback without data")
            return States.END

        await cq.answer()

        # Parse action
        parts = cq.data.split(":")
        if len(parts) < 3:
            await cq.edit_message_text(t("err_processing"))
            return States.END

        action = parts[1]  # "readings", "budget", or "close"
        period_id = int(parts[2])

        if action == "readings":
            # Route to electricity workflow
            return await _start_electricity_workflow(update, context, period_id)
        elif action == "budget":
            # Route to budget workflow
            return await _start_budget_workflow(update, context, period_id)
        else:
            await cq.edit_message_text(t("err_processing"))
            return States.END

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "in action selection")


async def _start_electricity_workflow(
    update: Update, context: ContextTypes.DEFAULT_TYPE, period_id: int
) -> int:
    """Initialize electricity workflow after action selection."""
    try:
        async with AsyncSessionLocal() as session:
            period_service = ServicePeriodService(session)
            period = await period_service.get_by_id(period_id)

            if not period:
                await update.callback_query.edit_message_text(t("err_processing"))
                return States.END

            # Store period info
            context.user_data["electricity_period_id"] = period_id
            context.user_data["electricity_period_name"] = period.name

            # Fetch previous period values for defaults
            defaults = await period_service.get_previous_period_defaults(period.start_date)

            # Store all previous period values for keyboard buttons
            context.user_data["electricity_previous_rate"] = defaults.electricity_rate
            context.user_data["electricity_previous_multiplier"] = defaults.electricity_multiplier
            context.user_data["electricity_previous_losses"] = defaults.electricity_losses

            # Ask for electricity_start
            default_start = defaults.electricity_end if defaults.electricity_end else "?"
            prompt = f"{t('prompt_meter_start')}\n\n{t('hint_previous_value', value=default_start)}"

            keyboard = _build_previous_value_keyboard(defaults.electricity_end)

            await update.callback_query.edit_message_text(t("msg_starting_readings"))
            await update.callback_query.message.reply_text(prompt, reply_markup=keyboard)

            return States.INPUT_METER_START

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "starting electricity workflow")


async def _start_budget_workflow(
    update: Update, context: ContextTypes.DEFAULT_TYPE, period_id: int
) -> int:
    """Initialize budget workflow after action selection."""
    try:
        async with AsyncSessionLocal() as session:
            period_service = ServicePeriodService(session)
            period = await period_service.get_by_id(period_id)

            if not period:
                await update.callback_query.edit_message_text(t("err_processing"))
                return States.END

            # Store period info
            context.user_data["budget_period_id"] = period_id
            context.user_data["budget_period_name"] = period.name

            # Fetch previous period for budget defaults
            from sqlalchemy import select

            prev_period_stmt = (
                select(ServicePeriod)
                .filter(ServicePeriod.start_date < period.start_date)
                .order_by(ServicePeriod.start_date.desc())
                .limit(1)
            )
            prev_result = await session.execute(prev_period_stmt)
            prev_period = prev_result.scalar_one_or_none()

            prev_year_budget = None
            prev_conservation_budget = None

            if prev_period:
                if prev_period.year_budget:
                    prev_year_budget = str(prev_period.year_budget)
                if prev_period.conservation_year_budget:
                    prev_conservation_budget = str(prev_period.conservation_year_budget)

            context.user_data["budget_previous_year_budget"] = prev_year_budget
            context.user_data["budget_previous_conservation_year_budget"] = prev_conservation_budget

            # Ask for year_budget
            default_text = ""
            if prev_year_budget:
                formatted_budget = format_currency(Decimal(prev_year_budget))
                default_text = t("hint_previous_value", value=formatted_budget)
            prompt = f"{t('prompt_budget_main')}{default_text}"

            keyboard = _build_previous_value_keyboard(prev_year_budget)

            await update.callback_query.edit_message_text(t("msg_starting_budget"))
            await update.callback_query.message.reply_text(prompt, reply_markup=keyboard)

            return States.INPUT_MAIN_BUDGET

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "starting budget workflow")


async def handle_electricity_meter_start(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle electricity meter start reading input."""
    try:
        if not update.message or not update.message.text:
            return States.INPUT_METER_START

        text = update.message.text.strip()
        value, valid = _validate_positive_decimal(text, allow_zero=True)

        if not valid:
            await update.message.reply_text(t("err_invalid_number"))
            return States.INPUT_METER_START

        context.user_data["electricity_start"] = value
        await update.message.reply_text(t("prompt_meter_end"))
        return States.INPUT_METER_END

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "in meter start input")


async def handle_electricity_meter_end(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle electricity meter end reading input."""
    try:
        if not update.message or not update.message.text:
            return States.INPUT_METER_END

        text = update.message.text.strip()
        value, valid = _validate_positive_decimal(text, allow_zero=True)

        if not valid:
            await update.message.reply_text(t("err_invalid_number"))
            return States.INPUT_METER_END

        electricity_start = context.user_data.get("electricity_start")
        if value <= electricity_start:
            await update.message.reply_text(t("err_meter_end_less_than_start"))
            return States.INPUT_METER_END

        context.user_data["electricity_end"] = value

        keyboard = _build_previous_value_keyboard(
            context.user_data.get("electricity_previous_multiplier")
        )
        await update.message.reply_text(t("prompt_multiplier"), reply_markup=keyboard)
        return States.INPUT_MULTIPLIER

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "in meter end input")


async def handle_electricity_multiplier(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle electricity multiplier input."""
    try:
        if not update.message or not update.message.text:
            return States.INPUT_MULTIPLIER

        text = update.message.text.strip()
        value, valid = _validate_positive_decimal(text)

        if not valid:
            await update.message.reply_text(t("err_invalid_number"))
            return States.INPUT_MULTIPLIER

        context.user_data["electricity_multiplier"] = value

        keyboard = _build_previous_value_keyboard(
            context.user_data.get("electricity_previous_rate")
        )
        await update.message.reply_text(t("prompt_rate"), reply_markup=keyboard)
        return States.INPUT_RATE

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "in multiplier input")


async def handle_electricity_rate(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle electricity rate input."""
    try:
        if not update.message or not update.message.text:
            return States.INPUT_RATE

        text = update.message.text.strip()
        value, valid = _validate_positive_decimal(text)

        if not valid:
            await update.message.reply_text(t("err_invalid_number"))
            return States.INPUT_RATE

        context.user_data["electricity_rate"] = value

        keyboard = _build_previous_value_keyboard(
            context.user_data.get("electricity_previous_losses")
        )
        await update.message.reply_text(t("prompt_losses"), reply_markup=keyboard)
        return States.INPUT_LOSSES

    except Exception as e:
        logger.error("Error in rate input: %s", e, exc_info=True)
        try:
            await update.message.reply_text(t("err_processing"))
        except Exception:
            pass
        return States.END


async def handle_electricity_losses(  # noqa: C901
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle electricity losses input and calculate total cost."""
    try:
        if not update.message or not update.message.text:
            return States.INPUT_LOSSES

        text = update.message.text.strip()
        value, valid = _validate_fraction(text)

        if not valid:
            await update.message.reply_text(t("err_invalid_losses"))
            return States.INPUT_LOSSES

        context.user_data["electricity_losses"] = value

        # Calculate total electricity cost
        async with AsyncSessionLocal() as session:
            bills_service = BillsService(session)
            period_service = ServicePeriodService(session)

            start = context.user_data.get("electricity_start")
            end = context.user_data.get("electricity_end")
            multiplier = context.user_data.get("electricity_multiplier")
            rate = context.user_data.get("electricity_rate")

            # calculate_total_electricity is a static method (no await needed)
            total_cost = BillsService.calculate_total_electricity(
                start, end, multiplier, rate, value
            )

            context.user_data["electricity_total_cost"] = total_cost

            # Proceed directly to distribute costs among owners (skip confirmation step)
            period_id = context.user_data.get("electricity_period_id")

            # Fetch the service period
            period = await period_service.get_by_id(period_id)
            if not period:
                await update.message.reply_text(t("err_processing"))
                return States.END

            # Guard: fail if any electricity bills already exist for this period
            existing_count = await bills_service.count_electricity_bills_for_period(period_id)
            if existing_count > 0:
                await update.message.reply_text(
                    t(
                        "err_electricity_bills_already_created",
                        period_name=period.name,
                        count=existing_count,
                    )
                )
                _clear_electricity_context(context)
                return States.END

            # Compute personal electricity bills from readings
            try:
                (
                    personal_bills,
                    personal_bills_sum,
                ) = await bills_service.calculate_personal_electricity_bills_from_readings(
                    service_period=period,
                    electricity_rate=rate,
                )
            except ValueError as exc:
                message = str(exc)
                if message.startswith("MISSING_READINGS:"):
                    details = message.removeprefix("MISSING_READINGS:")
                    await update.message.reply_text(
                        t("err_missing_electricity_readings", details=details)
                    )
                    _clear_electricity_context(context)
                    return States.END
                if message.startswith("INCONSISTENT_READINGS:"):
                    details = message.removeprefix("INCONSISTENT_READINGS:")
                    await update.message.reply_text(
                        t("err_inconsistent_electricity_readings", details=details)
                    )
                    _clear_electricity_context(context)
                    return States.END
                raise

            context.user_data["electricity_personal_bills"] = personal_bills
            context.user_data["electricity_personal_bills_sum"] = personal_bills_sum

            # Calculate shared cost (clip to 0 if personal bills exceed total)
            shared_cost = max(Decimal(0), total_cost - personal_bills_sum)

            context.user_data["electricity_shared_cost"] = shared_cost

            # Distribute shared costs
            owner_shares = await bills_service.distribute_shared_costs(shared_cost, period)

            context.user_data["electricity_owner_shares"] = owner_shares
            context.user_data["electricity_shared_cost"] = shared_cost

            # Show the proposed bills table with owner shares and summary (skip state 9)
            return await _show_electricity_bills_table(update, context)

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "in losses input")


async def _show_electricity_bills_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display proposed electricity bills table with percentages and ask for confirmation."""
    try:
        personal_bills: list[PersonalElectricityBill] = context.user_data.get(
            "electricity_personal_bills", []
        )
        owner_shares = context.user_data.get("electricity_owner_shares", [])

        # Personal bills section
        personal_lines: list[str] = []
        personal_total = Decimal("0")
        for bill in personal_bills:
            personal_total += bill.bill_amount
            amount_formatted = format_currency(bill.bill_amount)
            personal_lines.append(f"• {bill.owner_name} — {bill.property_name}: {amount_formatted}")

        if personal_lines:
            personal_table = f"{t('label_bill_electricity')}\n" + "\n".join(personal_lines)
            personal_table += f"\n\n*{t('label_total')}: {format_currency(personal_total)}*"
        else:
            personal_table = f"{t('label_bill_electricity')}\n{t('empty_bills')}"

        # Build bills table with percentages and Russian thousand separator formatting
        bills_text = ""
        total_share_weight = Decimal("0")
        total_bill_amount = Decimal("0")

        # First pass: calculate totals
        for share in owner_shares:
            total_share_weight += share.total_share_weight
            total_bill_amount += share.calculated_bill_amount

        # Second pass: build formatted table with percentages
        for share in owner_shares:
            # Calculate percentage
            if total_share_weight > 0:
                percentage = (share.total_share_weight / total_share_weight) * 100
            else:
                percentage = 0

            # Format amounts with Russian thousand separators
            amount_formatted = format_currency(share.calculated_bill_amount)
            bills_text += f"• {share.user_name}: {percentage:.2f}% → {amount_formatted}\n"

        # Add summary line with calculated total percentage
        total_amount_formatted = format_currency(total_bill_amount)
        total_percentage = (
            (total_share_weight / total_share_weight * 100) if total_share_weight > 0 else 0
        )
        bills_text += f"\n*{total_percentage:.2f}% → {total_amount_formatted}*"

        shared_table = f"{t('label_bill_shared_electricity')}\n" + bills_text
        message = t(
            "msg_confirm_electricity_bills",
            personal_table=personal_table,
            shared_table=shared_table,
        )

        buttons = [
            [
                InlineKeyboardButton(t("btn_create_bills"), callback_data="elec_bills:create"),
                InlineKeyboardButton(t("btn_cancel"), callback_data="elec_bills:cancel"),
            ]
        ]
        keyboard = InlineKeyboardMarkup(buttons)

        if update.callback_query:
            await update.callback_query.edit_message_text(
                message, reply_markup=keyboard, parse_mode="Markdown"
            )
        else:
            await update.message.reply_text(message, reply_markup=keyboard, parse_mode="Markdown")

        return States.CONFIRM_ELECTRICITY_BILLS

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "showing bills table")


async def handle_electricity_create_bills(  # noqa: C901
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Create shared electricity bills in the database."""
    try:
        cq = update.callback_query
        if not cq or not cq.data:
            return States.CONFIRM_ELECTRICITY_BILLS

        await cq.answer()

        if cq.data == "elec_bills:cancel":
            await cq.edit_message_text(t("msg_operation_cancelled"))
            return States.END

        if cq.data != "elec_bills:create":
            logger.warning("Unexpected callback data: %s", cq.data)
            return States.CONFIRM_ELECTRICITY_BILLS

        # Create bills
        async with AsyncSessionLocal() as session:
            try:
                period_service = ServicePeriodService(session)
                bills_service = BillsService(session)
                owner_shares = context.user_data.get("electricity_owner_shares", [])
                personal_bills = context.user_data.get("electricity_personal_bills", [])
                period_id = context.user_data.get("electricity_period_id")

                if not owner_shares or not period_id:
                    await cq.edit_message_text(t("err_processing"))
                    return States.END

                admin_user = context.user_data.get("authorized_admin")
                actor_id = admin_user.id if admin_user else None

                # Guard again: fail if bills already exist
                existing_count = await bills_service.count_electricity_bills_for_period(period_id)
                if existing_count > 0:
                    await cq.edit_message_text(
                        t(
                            "err_electricity_bills_already_created",
                            period_name=context.user_data.get("electricity_period_name", ""),
                            count=existing_count,
                        )
                    )
                    _clear_electricity_context(context)
                    return States.END

                # Update service period with electricity values
                await period_service.update_electricity_data(
                    period_id=period_id,
                    electricity_start=context.user_data.get("electricity_start"),
                    electricity_end=context.user_data.get("electricity_end"),
                    electricity_multiplier=context.user_data.get("electricity_multiplier"),
                    electricity_rate=context.user_data.get("electricity_rate"),
                    electricity_losses=context.user_data.get("electricity_losses"),
                    actor_id=actor_id,
                )

                (
                    personal_count,
                    shared_count,
                ) = await bills_service.create_personal_and_shared_electricity_bills(
                    period_id=period_id,
                    personal_bills=personal_bills,
                    owner_shares=owner_shares,
                    actor_id=actor_id,
                )

                # Confirm success with period name (send as reply to preserve message history)
                period_name = context.user_data.get("electricity_period_name", t("label_period"))
                message = t(
                    "msg_bills_created_electricity",
                    personal_count=personal_count,
                    shared_count=shared_count,
                    period_name=period_name,
                )
                await cq.message.reply_text(message)

                logger.info(
                    "Created electricity bills for period %d: personal=%d shared=%d",
                    period_id,
                    personal_count,
                    shared_count,
                )

                _clear_electricity_context(context)

                return States.END

            except Exception as e:
                await session.rollback()
                logger.error("Error creating bills: %s", e, exc_info=True)
                try:
                    await cq.edit_message_text(t("err_processing"))
                except Exception:
                    pass
                return States.END

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "in create bills handler")


async def handle_budget_main_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle year_budget (MAIN bills) input."""
    try:
        if not update.message or not update.message.text:
            return States.INPUT_MAIN_BUDGET

        text = update.message.text.strip()
        value, valid = _validate_positive_decimal(text, allow_zero=False)

        if not valid:
            await update.message.reply_text(t("err_invalid_number"))
            return States.INPUT_MAIN_BUDGET

        context.user_data["budget_year_budget"] = value

        # Ask for conservation_year_budget
        prev_conservation = context.user_data.get("budget_previous_conservation_year_budget")
        default_text = ""
        if prev_conservation:
            formatted_budget = format_currency(Decimal(prev_conservation))
            default_text = t("hint_previous_value", value=formatted_budget)
        prompt = f"{t('prompt_budget_conservation')}{default_text}"

        keyboard = _build_previous_value_keyboard(prev_conservation)
        await update.message.reply_text(prompt, reply_markup=keyboard)

        return States.INPUT_CONSERVATION_BUDGET

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "in main budget input")


async def handle_budget_conservation_input(  # noqa: C901
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle conservation_year_budget input and calculate bills."""
    try:
        if not update.message or not update.message.text:
            return States.INPUT_CONSERVATION_BUDGET

        text = update.message.text.strip()
        value, valid = _validate_positive_decimal(text, allow_zero=False)

        if not valid:
            await update.message.reply_text(t("err_invalid_number"))
            return States.INPUT_CONSERVATION_BUDGET

        context.user_data["budget_conservation_year_budget"] = value

        # Calculate bills
        async with AsyncSessionLocal() as session:
            period_service = ServicePeriodService(session)
            bills_service = BillsService(session)

            period_id = context.user_data.get("budget_period_id")
            period = await period_service.get_by_id(period_id)

            if not period:
                await update.message.reply_text(t("err_processing"))
                return States.END

            year_budget = context.user_data["budget_year_budget"]
            conservation_year_budget = value

            # Calculate both bill types
            main_calculations = await bills_service.calculate_main_bills(
                year_budget, period.period_months
            )
            conservation_calculations = await bills_service.calculate_conservation_bills(
                conservation_year_budget, period.period_months
            )

            # Enrich with usernames (fetch all active owners for complete list)
            from sqlalchemy import select

            # Get all user IDs from calculations
            main_by_user = dict(main_calculations)
            conservation_by_user = dict(conservation_calculations)
            all_user_ids_set = set(main_by_user.keys()) | set(conservation_by_user.keys())

            # Fetch all users for mapping
            stmt = (
                select(User).where(User.id.in_(all_user_ids_set))
                if all_user_ids_set
                else select(User).filter(False)
            )
            result = await session.execute(stmt)
            users = {user.id: user.name for user in result.scalars().all()}

            # Transform into OwnerShare objects with names, including owners with 0 amounts
            main_shares = [
                OwnerShare(
                    user_id=user_id,
                    user_name=users.get(user_id, f"User #{user_id}"),
                    total_share_weight=Decimal("0"),  # Not used for budget bills display
                    calculated_bill_amount=main_by_user.get(user_id, Decimal("0")),
                )
                for user_id in sorted(main_by_user.keys()) or all_user_ids_set
            ]
            conservation_shares = [
                OwnerShare(
                    user_id=user_id,
                    user_name=users.get(user_id, f"User #{user_id}"),
                    total_share_weight=Decimal("0"),  # Not used for budget bills display
                    calculated_bill_amount=conservation_by_user.get(user_id, Decimal("0")),
                )
                for user_id in sorted(conservation_by_user.keys())
            ]

            context.user_data["budget_main_calculations"] = main_shares
            context.user_data["budget_conservation_calculations"] = conservation_shares

            # Show confirmation table
            return await _show_budget_bills_table(update, context)

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "in conservation budget input")


async def _show_budget_bills_table(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Display proposed budget bills table and ask for confirmation."""
    try:
        main_shares = context.user_data.get("budget_main_calculations", [])
        conservation_shares = context.user_data.get("budget_conservation_calculations", [])
        year_budget = context.user_data.get("budget_year_budget", Decimal("0"))
        conservation_year_budget = context.user_data.get(
            "budget_conservation_year_budget", Decimal("0")
        )
        period_name = context.user_data.get("budget_period_name", "")

        # Get period months from service period
        async with AsyncSessionLocal() as session:
            period_service = ServicePeriodService(session)
            period_id = context.user_data.get("budget_period_id")
            period = await period_service.get_by_id(period_id)
            period_months = period.period_months if period else 1

        # Calculate expected totals based on period
        expected_main_total = (year_budget / 12) * period_months
        expected_conservation_total = (conservation_year_budget / 12) * period_months

        # Add budget info header (plain text to avoid Markdown parsing issues)
        budget_info = (
            f"📊 {t('label_period')}: {period_name} ({period_months} {t('label_months_short')})\n"
        )
        budget_info += f"💰 {t('prompt_budget_main')} {format_currency(year_budget)}\n"
        budget_info += (
            f"💰 {t('prompt_budget_conservation')} {format_currency(conservation_year_budget)}\n"
        )
        budget_info += (
            f"📅 {t('title_expected_period_total')} ({period_months} {t('label_months_short')}):\n"
        )
        budget_info += f"  • {t('label_bill_main')}: {format_currency(expected_main_total)}\n"
        budget_info += f"  • {t('label_bill_conservation')}: {format_currency(expected_conservation_total)}\n\n"

        # Build MAIN bills table with usernames and percentages (consistent with electricity format)
        main_text = f"{t('title_bills_header_main')}:\n"
        main_total = Decimal("0")

        # First pass: calculate total for MAIN
        for share in main_shares:
            main_total += share.calculated_bill_amount

        # Second pass: build formatted table with percentages for MAIN (based on amount, not share_weight)
        for share in main_shares:
            if main_total > 0:
                percentage = (share.calculated_bill_amount / main_total) * 100
            else:
                percentage = 0
            amount_formatted = format_currency(share.calculated_bill_amount)
            main_text += f"• {share.user_name}: {percentage:.2f}% → {amount_formatted}\n"

        if not main_shares:
            main_text += t("empty_bills") + "\n"
        else:
            main_total_formatted = format_currency(main_total)
            main_text += f"\n*100.00% → {main_total_formatted}*\n"

        # Build CONSERVATION bills table with usernames and percentages (consistent with electricity format)
        conservation_text = f"\n{t('title_bills_header_conservation')}:\n"
        conservation_total = Decimal("0")

        # First pass: calculate total for CONSERVATION
        for share in conservation_shares:
            conservation_total += share.calculated_bill_amount

        # Second pass: build formatted table with percentages for CONSERVATION (based on amount, not share_weight)
        for share in conservation_shares:
            if conservation_total > 0:
                percentage = (share.calculated_bill_amount / conservation_total) * 100
            else:
                percentage = 0
            amount_formatted = format_currency(share.calculated_bill_amount)
            conservation_text += f"• {share.user_name}: {percentage:.2f}% → {amount_formatted}\n"

        if not conservation_shares:
            conservation_text += t("empty_bills") + "\n"
        else:
            conservation_total_formatted = format_currency(conservation_total)
            conservation_text += f"\n*100.00% → {conservation_total_formatted}*\n"

        message = budget_info + main_text + conservation_text + f"\n{t('msg_confirm_budget_bills')}"

        buttons = [
            [
                InlineKeyboardButton(t("btn_create_bills"), callback_data="budget_bills:create"),
                InlineKeyboardButton(t("btn_cancel"), callback_data="budget_bills:cancel"),
            ]
        ]
        keyboard = InlineKeyboardMarkup(buttons)

        await update.message.reply_text(message, parse_mode="Markdown", reply_markup=keyboard)

        return States.CONFIRM_BUDGET_BILLS

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "showing budget bills table")


async def handle_budget_create_bills(  # noqa: C901
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Create MAIN and CONSERVATION bills in the database."""
    try:
        cq = update.callback_query
        if not cq or not cq.data:
            return States.END

        await cq.answer()

        if cq.data == "budget_bills:cancel":
            await cq.edit_message_text(t("msg_operation_cancelled"))
            _clear_budget_context(context)
            return States.END

        if cq.data != "budget_bills:create":
            return States.END

        # Create bills
        async with AsyncSessionLocal() as session:
            bills_service = BillsService(session)
            period_service = ServicePeriodService(session)

            period_id = context.user_data.get("budget_period_id")
            period_name = context.user_data.get("budget_period_name")
            main_calculations = context.user_data.get("budget_main_calculations", [])
            conservation_calculations = context.user_data.get(
                "budget_conservation_calculations", []
            )
            year_budget = context.user_data.get("budget_year_budget")
            conservation_year_budget = context.user_data.get("budget_conservation_year_budget")

            admin_user = context.user_data.get("authorized_admin")
            actor_id = admin_user.id if admin_user else None

            # Create MAIN bills
            main_count = await bills_service.create_main_bills(
                period_id=period_id,
                calculations=main_calculations,
                actor_id=actor_id,
            )

            # Create CONSERVATION bills
            conservation_count = await bills_service.create_conservation_bills(
                period_id=period_id,
                calculations=conservation_calculations,
                actor_id=actor_id,
            )

            # Update period with budget data
            await period_service.update_budget_data(
                period_id=period_id,
                year_budget=year_budget,
                conservation_year_budget=conservation_year_budget,
                actor_id=actor_id,
            )

            # Send success message as reply to preserve calculations table
            message = t(
                "msg_bills_created_both",
                main_count=main_count,
                conservation_count=conservation_count,
                period_name=period_name,
            )
            await cq.message.reply_text(message)

            _clear_budget_context(context)
            return States.END

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "in create budget bills handler")


__all__ = [
    "States",
    "handle_bills_command",
    "handle_bills_cancel",
    "handle_period_selection",
    "handle_action_selection",
    "handle_electricity_meter_start",
    "handle_electricity_meter_end",
    "handle_electricity_multiplier",
    "handle_electricity_rate",
    "handle_electricity_losses",
    "handle_electricity_create_bills",
    "handle_budget_main_input",
    "handle_budget_conservation_input",
    "handle_budget_create_bills",
]

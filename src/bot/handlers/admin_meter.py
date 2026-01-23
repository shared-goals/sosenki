"""Electricity meter reading management handlers with conversation state machine."""

import asyncio
import logging
from datetime import date
from decimal import Decimal

import httpx
from telegram import (
    InlineKeyboardButton,
    InlineKeyboardMarkup,
    KeyboardButton,
    ReplyKeyboardMarkup,
    Update,
)
from telegram.error import BadRequest
from telegram.ext import ContextTypes

from src.bot.handlers.error_utils import handle_conversation_error
from src.models import ElectricityReading
from src.services import AsyncSessionLocal
from src.services.auth_service import verify_bot_admin_or_staff_authorization
from src.services.electricity_reading_service import ElectricityReadingService
from src.services.localizer import t
from src.utils.parsers import parse_date, parse_russian_decimal

logger = logging.getLogger(__name__)


# Conversation state constants
class States:
    """Conversation states for meter readings workflow."""

    END = -1
    SELECT_PROPERTY = 20
    SELECT_ACTION = 21
    CONFIRM_DELETE = 22
    ENTER_DATE = 23
    ENTER_VALUE = 24
    CONFIRM = 25


# Context data keys
_METER_KEYS = [
    "meter_admin_id",
    "meter_property_id",
    "meter_property_name",
    "meter_reading_id",
    "meter_action",
    "meter_date",
    "meter_value",
    "meter_previous_reading",
    "authorized_admin",
]


def _clear_meter_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all meter-related context data."""
    for key in _METER_KEYS:
        context.user_data.pop(key, None)


def _build_suggested_date_keyboard(suggested_date: date) -> ReplyKeyboardMarkup | None:
    """Build a keyboard with suggested date button if available.

    Args:
        suggested_date: Date to suggest (formatted as dd.mm.yyyy)

    Returns:
        ReplyKeyboardMarkup with suggested date or None if date is invalid
    """
    if not suggested_date:
        return None
    formatted_date = suggested_date.strftime("%d.%m.%Y")
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text=formatted_date)]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )


def _build_suggested_value_keyboard(suggested_value: Decimal) -> ReplyKeyboardMarkup | None:
    """Build a keyboard with suggested value button if available.

    Args:
        suggested_value: Decimal value to suggest (formatted as plain number)

    Returns:
        ReplyKeyboardMarkup with suggested value or None if value is invalid/zero
    """
    if not suggested_value or suggested_value <= 0:
        return None
    # Format as plain decimal without symbols (e.g., "1000.5")
    formatted_value = str(suggested_value)
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text=formatted_value)]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )


async def _show_property_selection(  # noqa: C901
    update: Update, context: ContextTypes.DEFAULT_TYPE, success_message: str = None
) -> int:
    """Show property selection screen with optional success message.

    Args:
        update: Telegram update object
        context: Context with user_data
        success_message: Optional success message to show before property list

    Returns:
        SELECT_PROPERTY state or END if no properties
    """
    try:
        async with AsyncSessionLocal() as session:
            service = ElectricityReadingService(session)
            properties_with_readings = await service.get_properties_with_latest_readings()

            if not properties_with_readings:
                error_msg = t("err_no_properties")
                if update.callback_query:
                    await update.callback_query.edit_message_text(error_msg)
                elif update.message:
                    await update.message.reply_text(error_msg)
                return States.END

            # Split properties into two groups: with readings and without readings
            properties_with_data = []
            properties_without_data = []

            for property_obj, latest_reading in properties_with_readings:
                if latest_reading:
                    properties_with_data.append((property_obj, latest_reading))
                else:
                    properties_without_data.append((property_obj, None))

            # Build property selection keyboard - show properties with readings first
            keyboard = []
            for property_obj, latest_reading in properties_with_data:
                button_text = f"📊 {property_obj.property_name} ({latest_reading.reading_date.strftime('%d.%m.%Y')}): {latest_reading.reading_value} {t('label_unit_kwh')}"
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=button_text,
                            callback_data=f"meter_property_{property_obj.id}",
                        )
                    ]
                )

            # Add button to show properties without readings (if any exist)
            if properties_without_data:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=t("btn_show_properties_without_readings"),
                            callback_data="meter_show_empty",
                        )
                    ]
                )

            keyboard.append(
                [InlineKeyboardButton(text=t("btn_cancel"), callback_data="meter_cancel")]
            )

            message_text = t("prompt_select_property_meter")
            if success_message:
                message_text = f"{success_message}\n\n{message_text}"

            reply_markup = InlineKeyboardMarkup(keyboard)

            # Check if we have a callback query (from button click) or message (from command)
            if update.callback_query is not None:
                try:
                    await update.callback_query.edit_message_text(
                        message_text,
                        reply_markup=reply_markup,
                        parse_mode="HTML",
                    )
                except BadRequest as e:
                    # If message is not modified (pressing Cancel twice on same step)
                    # Replace with cancellation message and end conversation
                    if "message is not modified" in str(e).lower():
                        await update.callback_query.edit_message_text(
                            t("msg_operation_cancelled"),
                            parse_mode="HTML",
                        )
                        return States.END
                    raise
            elif update.message is not None:
                await update.message.reply_text(
                    message_text,
                    reply_markup=reply_markup,
                    parse_mode="HTML",
                )

            return States.SELECT_PROPERTY

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "showing property selection")


async def handle_meter_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel meter readings workflow or exit from property selection.

    If called from property selection (callback_query), go back to property list.
    If called from command (message), end conversation completely.

    Returns:
        SELECT_PROPERTY state (from callbacks) or END state (from messages)
    """
    # Check if this is from property selection (callback) or initial command (message)
    if update.callback_query:
        # User clicked cancel on property selection - loop back to list
        return await _show_property_selection(update, context, t("msg_operation_cancelled"))
    else:
        # User cancelled from other entry point - end conversation
        _clear_meter_context(context)
        if update.message:
            await update.message.reply_text(t("msg_operation_cancelled"))
        return States.END


async def handle_meter_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle /meter command to start meter readings workflow.

    Authorization: is_administrator OR is_staff

    Returns:
        SELECT_PROPERTY state or END if unauthorized
    """
    if not update.message or not update.message.from_user:
        return States.END

    user = update.message.from_user

    # Verify authorization
    try:
        authorized_admin = await verify_bot_admin_or_staff_authorization(user.id)
        if not authorized_admin:
            await update.message.reply_text(t("err_not_authorized"))
            return States.END

        # Store actor context (telegram user id)
        # NOTE: existing services/audit logs treat actor_id as telegram_id.
        context.user_data["meter_admin_id"] = user.id
        context.user_data["authorized_admin"] = authorized_admin

        # Show property selection using helper
        return await _show_property_selection(update, context)

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(
            e, update, "in /meter command", cleanup_fn=_clear_meter_context, context=context
        )


async def handle_show_empty_properties(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Show properties without readings.

    Returns:
        SELECT_PROPERTY state
    """
    query = update.callback_query
    if not query:
        return States.END

    await query.answer()

    try:
        async with AsyncSessionLocal() as session:
            # Get all properties with latest readings
            service = ElectricityReadingService(session)
            properties_with_readings = await service.get_properties_with_latest_readings()

            # Filter to show only properties without readings
            properties_without_data = [
                (property_obj, latest_reading)
                for property_obj, latest_reading in properties_with_readings
                if not latest_reading
            ]

            if not properties_without_data:
                await query.edit_message_text(t("msg_all_properties_have_readings"))
                _clear_meter_context(context)
                return States.END

            # Build keyboard with properties without readings
            keyboard = []
            for property_obj, _ in properties_without_data:
                button_text = f"📊 {property_obj.property_name} ({t('empty_data')})"
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=button_text,
                            callback_data=f"meter_property_{property_obj.id}",
                        )
                    ]
                )

            keyboard.append(
                [InlineKeyboardButton(text=t("btn_cancel"), callback_data="meter_cancel")]
            )

            await query.edit_message_text(
                t("prompt_select_property_meter"),
                reply_markup=InlineKeyboardMarkup(keyboard),
            )

            return States.SELECT_PROPERTY

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(
            e, update, "showing empty properties", cleanup_fn=_clear_meter_context, context=context
        )


async def handle_property_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle property selection from callback query.

    Returns:
        SELECT_ACTION state
    """
    query = update.callback_query
    if not query or not query.data:
        return States.END

    await query.answer()

    if query.data == "meter_cancel":
        return await handle_meter_cancel(update, context)

    # Extract property_id from callback data
    if not query.data.startswith("meter_property_"):
        await query.edit_message_text(t("err_invalid_action"))
        _clear_meter_context(context)
        return States.END

    property_id = int(query.data.replace("meter_property_", ""))
    context.user_data["meter_property_id"] = property_id

    try:
        async with AsyncSessionLocal() as session:
            service = ElectricityReadingService(session)

            # Get property details
            from sqlalchemy import select

            from src.models.property import Property

            stmt = select(Property).where(Property.id == property_id)
            result = await session.execute(stmt)
            property_obj = result.scalar_one_or_none()

            if not property_obj:
                await query.edit_message_text(t("err_no_properties"))
                _clear_meter_context(context)
                return States.END

            context.user_data["meter_property_name"] = property_obj.property_name

            # Get latest reading
            latest_reading = await service.get_latest_reading_for_property(property_id)
            context.user_data["meter_previous_reading"] = latest_reading

            # Build action selection keyboard
            keyboard = []

            # Always show "New reading" action
            keyboard.append(
                [InlineKeyboardButton(text=t("btn_meter_new"), callback_data="meter_action_new")]
            )

            # Show edit/delete only if there's a reading
            if latest_reading:
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=t("btn_meter_edit"), callback_data="meter_action_edit"
                        )
                    ]
                )
                keyboard.append(
                    [
                        InlineKeyboardButton(
                            text=t("btn_meter_delete"), callback_data="meter_action_delete"
                        )
                    ]
                )

            keyboard.append(
                [InlineKeyboardButton(text=t("btn_cancel"), callback_data="meter_cancel")]
            )

            # Show current reading if exists
            property_name = property_obj.property_name
            message_text = t("prompt_select_meter_action")
            if latest_reading:
                message_text = (
                    t("msg_meter_current_reading").format(
                        property_name=property_name,
                        date=latest_reading.reading_date.strftime("%d.%m.%Y"),
                        value=latest_reading.reading_value,
                    )
                    + "\n\n"
                    + message_text
                )
            else:
                message_text = t("msg_meter_no_previous_reading") + "\n\n" + message_text

            await query.edit_message_text(
                message_text,
                reply_markup=InlineKeyboardMarkup(keyboard),
                parse_mode="HTML",
            )

            return States.SELECT_ACTION

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(
            e, update, "in property selection", cleanup_fn=_clear_meter_context, context=context
        )


async def handle_action_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  # noqa: C901
    """Handle action selection (new, edit, delete).

    Returns:
        CONFIRM_DELETE, ENTER_DATE, or END state
    """
    query = update.callback_query
    if not query or not query.data:
        return States.END

    await query.answer()

    if query.data == "meter_cancel":
        return await handle_meter_cancel(update, context)

    # Parse action
    if not query.data.startswith("meter_action_"):
        await query.edit_message_text(t("err_invalid_action"))
        _clear_meter_context(context)
        return States.END

    action = query.data.replace("meter_action_", "")
    context.user_data["meter_action"] = action

    property_name = context.user_data.get("meter_property_name", "")
    previous_reading = context.user_data.get("meter_previous_reading")

    if action == "delete":
        # Confirm deletion
        if not previous_reading:
            await query.edit_message_text(t("err_meter_reading_not_found"))
            _clear_meter_context(context)
            return States.END

        # Set meter_reading_id for delete confirmation
        context.user_data["meter_reading_id"] = previous_reading.id

        keyboard = [
            [InlineKeyboardButton(text=t("btn_confirm"), callback_data="meter_confirm_delete")],
            [InlineKeyboardButton(text=t("btn_cancel"), callback_data="meter_cancel")],
        ]

        await query.edit_message_text(
            t("prompt_confirm_delete_reading").format(
                property_name=property_name,
                date=previous_reading.reading_date.strftime("%d.%m.%Y"),
                value=previous_reading.reading_value,
            ),
            reply_markup=InlineKeyboardMarkup(keyboard),
            parse_mode="HTML",
        )

        return States.CONFIRM_DELETE

    elif action in ("new", "edit"):
        # Create service for fetching readings
        async with AsyncSessionLocal() as session:
            electricity_reading_service = ElectricityReadingService(session)
            property_id = context.user_data.get("meter_property_id")

            # Get latest readings
            latest_property_reading = (
                await electricity_reading_service.get_latest_reading_for_property(property_id)
            )
            latest_global_reading = await electricity_reading_service.get_latest_reading_globally()

            # Store property reading for later use (value keyboard in handle_date_input)
            context.user_data["meter_latest_property_reading"] = latest_property_reading

            # For edit action: store the reading id and get the reading BEFORE it for validation
            if action == "edit" and latest_property_reading:
                context.user_data["meter_reading_id"] = latest_property_reading.id
                # Get the reading that came before this one (for validation)
                from sqlalchemy import select

                stmt = (
                    select(ElectricityReading)
                    .where(
                        ElectricityReading.property_id == property_id,
                        ElectricityReading.reading_date < latest_property_reading.reading_date,
                        ElectricityReading.id != latest_property_reading.id,
                    )
                    .order_by(ElectricityReading.reading_date.desc())
                    .limit(1)
                )
                result = await session.execute(stmt)
                previous_for_validation = result.scalar_one_or_none()
                context.user_data["meter_previous_reading"] = previous_for_validation
            else:
                # For new readings, use the latest reading as the comparison baseline
                context.user_data["meter_previous_reading"] = latest_property_reading

            # Build message text (show the reading being edited)
            if latest_property_reading:
                message_text = t("msg_meter_previous_reading").format(
                    property_name=property_name,
                    date=latest_property_reading.reading_date.strftime("%d.%m.%Y"),
                    value=latest_property_reading.reading_value,
                )
            else:
                message_text = t("msg_meter_no_previous_reading")

            await query.edit_message_text(message_text, parse_mode="HTML")

            # Send date keyboard with globally latest reading date (only if we have a global reading)
            if latest_global_reading:
                suggested_date_keyboard = _build_suggested_date_keyboard(
                    latest_global_reading.reading_date
                )
                if suggested_date_keyboard:
                    await query.message.reply_text(
                        t("prompt_enter_reading_date"),
                        reply_markup=suggested_date_keyboard,
                    )

            return States.ENTER_DATE

    else:
        await query.edit_message_text(t("err_invalid_action"))
        _clear_meter_context(context)
        return States.END


async def handle_delete_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle delete confirmation.

    Returns:
        END state
    """
    query = update.callback_query
    if not query or not query.data:
        return States.END

    await query.answer()

    if query.data == "meter_cancel":
        return await handle_meter_cancel(update, context)

    if query.data != "meter_confirm_delete":
        await query.edit_message_text(t("err_invalid_action"))
        _clear_meter_context(context)
        return States.END

    reading_id = context.user_data.get("meter_reading_id")
    admin_id = context.user_data.get("meter_admin_id")
    property_name = context.user_data.get("meter_property_name", "")

    if not reading_id or not admin_id:
        await query.edit_message_text(t("err_meter_reading_not_found"))
        _clear_meter_context(context)
        return States.END

    try:
        async with AsyncSessionLocal() as session:
            service = ElectricityReadingService(session)

            # Get reading before deletion (for message)
            reading = await service.get_reading_by_id(reading_id)
            if not reading:
                await query.edit_message_text(t("err_meter_reading_not_found"))
                _clear_meter_context(context)
                return States.END

            reading_date = reading.reading_date

            # Delete reading
            await service.delete_reading(reading_id=reading_id, actor_id=admin_id)
            await session.commit()

            success_msg = t("msg_meter_reading_deleted").format(
                property_name=property_name,
                date=reading_date.strftime("%d.%m.%Y"),
            )

            # Clear operation-specific context but keep admin context
            for key in [
                "meter_property_id",
                "meter_property_name",
                "meter_reading_id",
                "meter_action",
                "meter_date",
                "meter_value",
                "meter_previous_reading",
            ]:
                context.user_data.pop(key, None)

            # Return to property selection
            return await _show_property_selection(update, context, success_msg)

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(
            e, update, "deleting reading", cleanup_fn=_clear_meter_context, context=context
        )


async def handle_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle date input.

    Returns:
        ENTER_VALUE state or END on error
    """
    if not update.message or not update.message.text:
        return States.END

    text = update.message.text.strip()

    # Validate date format
    try:
        reading_date = parse_date(text)
        if reading_date is None:
            await update.message.reply_text(t("err_invalid_date_format"))
            return States.ENTER_DATE
    except ValueError:
        await update.message.reply_text(t("err_invalid_date_format"))
        return States.ENTER_DATE

    context.user_data["meter_date"] = reading_date

    # Build suggested value keyboard from property's latest reading
    latest_property_reading = context.user_data.get("meter_latest_property_reading")
    suggested_value_keyboard = None
    if latest_property_reading:
        suggested_value_keyboard = _build_suggested_value_keyboard(
            latest_property_reading.reading_value
        )

    # Prompt for value with optional suggestion
    if suggested_value_keyboard:
        await update.message.reply_text(
            t("prompt_enter_reading_value"),
            reply_markup=suggested_value_keyboard,
        )
    else:
        await update.message.reply_text(t("prompt_enter_reading_value"))

    return States.ENTER_VALUE


async def handle_value_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle value input and show confirmation.

    Returns:
        CONFIRM state or ENTER_VALUE on error
    """
    if not update.message or not update.message.text:
        return States.END

    text = update.message.text.strip()

    # Parse value
    try:
        reading_value = parse_russian_decimal(text)
        if reading_value is None or reading_value <= 0:
            await update.message.reply_text(t("err_meter_value_not_positive"))
            return States.ENTER_VALUE
    except ValueError:
        await update.message.reply_text(t("err_invalid_number"))
        return States.ENTER_VALUE

    context.user_data["meter_value"] = reading_value

    property_name = context.user_data.get("meter_property_name", "")
    reading_date = context.user_data.get("meter_date")
    action = context.user_data.get("meter_action")
    previous_reading = context.user_data.get("meter_previous_reading")

    if not reading_date or not action:
        await update.message.reply_text(t("err_processing"))
        _clear_meter_context(context)
        return States.END

    # Validate that new reading is greater than or equal to previous
    if previous_reading and reading_value < previous_reading.reading_value:
        await update.message.reply_text(
            t("err_meter_value_less_than_previous").format(
                value=reading_value,
                previous=previous_reading.reading_value,
            )
        )
        return States.ENTER_VALUE

    # Build confirmation message
    if action == "new":
        # Show what will be created
        if previous_reading:
            difference = reading_value - previous_reading.reading_value
            message_text = t("prompt_confirm_meter_new").format(
                property_name=property_name,
                date=reading_date.strftime("%d.%m.%Y"),
                value=reading_value,
                previous=previous_reading.reading_value,
                difference=difference,
            )
        else:
            message_text = t("prompt_confirm_meter_new_first").format(
                property_name=property_name,
                date=reading_date.strftime("%d.%m.%Y"),
                value=reading_value,
            )
    else:  # edit
        # Show what will be changed
        # For edit, we need the current reading being edited (not the validation baseline)
        reading_id = context.user_data.get("meter_reading_id")
        current_reading = None

        if reading_id:
            async with AsyncSessionLocal() as session:
                electricity_reading_service = ElectricityReadingService(session)
                current_reading = await electricity_reading_service.get_reading_by_id(reading_id)

        if current_reading:
            message_text = t("prompt_confirm_meter_edit").format(
                property_name=property_name,
                old_date=current_reading.reading_date.strftime("%d.%m.%Y"),
                old_value=current_reading.reading_value,
                new_date=reading_date.strftime("%d.%m.%Y"),
                new_value=reading_value,
            )
        else:
            message_text = t("prompt_confirm_meter_edit").format(
                property_name=property_name,
                old_date="-",
                old_value="-",
                new_date=reading_date.strftime("%d.%m.%Y"),
                new_value=reading_value,
            )

    keyboard = [
        [InlineKeyboardButton(text=t("btn_confirm"), callback_data="meter_confirm_save")],
        [InlineKeyboardButton(text=t("btn_cancel"), callback_data="meter_cancel")],
    ]

    await update.message.reply_text(
        message_text,
        reply_markup=InlineKeyboardMarkup(keyboard),
        parse_mode="HTML",
    )

    return States.CONFIRM


async def handle_final_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  # noqa: C901
    """Handle final confirmation and save reading.

    Returns:
        END state
    """
    query = update.callback_query
    if not query or not query.data:
        return States.END

    await query.answer()

    if query.data == "meter_cancel":
        return await handle_meter_cancel(update, context)

    if query.data != "meter_confirm_save":
        await query.edit_message_text(t("err_invalid_action"))
        _clear_meter_context(context)
        return States.END

    property_id = context.user_data.get("meter_property_id")
    property_name = context.user_data.get("meter_property_name", "")
    reading_date = context.user_data.get("meter_date")
    reading_value = context.user_data.get("meter_value")
    admin_id = context.user_data.get("meter_admin_id")
    action = context.user_data.get("meter_action")
    reading_id = context.user_data.get("meter_reading_id")
    previous_reading = context.user_data.get("meter_previous_reading")

    if not property_id or not reading_date or not reading_value or not admin_id or not action:
        await query.edit_message_text(t("err_processing"))
        _clear_meter_context(context)
        return States.END

    try:
        async with AsyncSessionLocal() as session:
            service = ElectricityReadingService(session)

            success_msg = None

            if action == "new":
                # Create new reading
                try:
                    new_reading = await service.create_reading(
                        property_id=property_id,
                        reading_date=reading_date,
                        reading_value=reading_value,
                        actor_id=admin_id,
                    )
                    await session.commit()

                    # Calculate difference
                    if previous_reading:
                        difference = new_reading.reading_value - previous_reading.reading_value
                    else:
                        difference = new_reading.reading_value

                    success_msg = t("msg_meter_reading_created").format(
                        property_name=property_name,
                        date=reading_date.strftime("%d.%m.%Y"),
                        value=reading_value,
                        difference=difference,
                    )

                except ValueError as e:
                    # Validation error from service
                    if "must be greater than previous" in str(e):
                        # Extract previous value from error message
                        error_msg = None
                        if previous_reading:
                            error_msg = t("err_meter_value_less_than_previous").format(
                                value=reading_value,
                                previous=previous_reading.reading_value,
                            )
                        else:
                            error_msg = str(e)

                        # Clear only operation context, keep admin context
                        for key in [
                            "meter_property_id",
                            "meter_property_name",
                            "meter_reading_id",
                            "meter_action",
                            "meter_date",
                            "meter_value",
                            "meter_previous_reading",
                        ]:
                            context.user_data.pop(key, None)

                        # Show error and loop back to property selection
                        return await _show_property_selection(update, context, error_msg)
                    else:
                        raise

            elif action == "edit":
                # Update existing reading
                if not reading_id:
                    await query.edit_message_text(t("err_meter_reading_not_found"))
                    # Clear only operation context, keep admin context
                    for key in [
                        "meter_property_id",
                        "meter_property_name",
                        "meter_reading_id",
                        "meter_action",
                        "meter_date",
                        "meter_value",
                        "meter_previous_reading",
                    ]:
                        context.user_data.pop(key, None)
                    return States.END

                try:
                    await service.update_reading(
                        reading_id=reading_id,
                        reading_date=reading_date,
                        reading_value=reading_value,
                        actor_id=admin_id,
                    )
                    await session.commit()

                    # Calculate difference (new vs old)
                    if previous_reading:
                        difference = reading_value - previous_reading.reading_value
                        difference_str = f"+{difference}" if difference >= 0 else str(difference)
                    else:
                        difference_str = str(reading_value)

                    success_msg = t("msg_meter_reading_updated").format(
                        property_name=property_name,
                        date=reading_date.strftime("%d.%m.%Y"),
                        value=reading_value,
                        difference=difference_str,
                    )

                except ValueError as e:
                    if "must be greater than previous" in str(e):
                        # Extract previous value if available
                        error_msg = None
                        if previous_reading:
                            error_msg = t("err_meter_value_less_than_previous").format(
                                value=reading_value,
                                previous=previous_reading.reading_value,
                            )
                        else:
                            error_msg = str(e)

                        # Clear only operation context, keep admin context
                        for key in [
                            "meter_property_id",
                            "meter_property_name",
                            "meter_reading_id",
                            "meter_action",
                            "meter_date",
                            "meter_value",
                            "meter_previous_reading",
                        ]:
                            context.user_data.pop(key, None)

                        # Show error and loop back to property selection
                        return await _show_property_selection(update, context, error_msg)
                    else:
                        raise

            # Clear operation-specific context but keep admin context
            for key in [
                "meter_property_id",
                "meter_property_name",
                "meter_reading_id",
                "meter_action",
                "meter_date",
                "meter_value",
                "meter_previous_reading",
            ]:
                context.user_data.pop(key, None)

            # Return to property selection with success message
            return await _show_property_selection(update, context, success_msg)

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(
            e, update, "processing meter reading", cleanup_fn=_clear_meter_context, context=context
        )

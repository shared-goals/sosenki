"""Payout (transaction) management handlers with conversation state machine."""

import asyncio
import logging
from datetime import date
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
from src.models.account import AccountType
from src.services import AsyncSessionLocal
from src.services.auth_service import verify_bot_admin_authorization
from src.services.locale_service import format_currency
from src.services.localizer import t
from src.services.notification_service import NotificationService
from src.services.transaction_service import TransactionService
from src.utils.parsers import parse_date, parse_russian_decimal

logger = logging.getLogger(__name__)


# Conversation state constants
class States:
    """Conversation states for payout workflow."""

    END = -1
    SELECT_FROM = 1
    SELECT_TO = 2
    ENTER_AMOUNT = 3
    ENTER_TRANSACTION_DATE = 4
    ENTER_DESCRIPTION = 5
    CONFIRM = 6


# Context data keys
_PAYOUT_KEYS = [
    "payout_admin_id",
    "payout_from_account",
    "payout_to_account",
    "payout_amount",
    "payout_date",
    "payout_description",
    "authorized_admin",
]


def _clear_payout_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all payout-related context data."""
    if context.user_data is None:
        return
    for key in _PAYOUT_KEYS:
        context.user_data.pop(key, None)


def _validate_positive_decimal(text: str) -> tuple[Decimal | None, bool]:
    """Validate and parse a positive decimal number.

    Args:
        text: Input text to parse

    Returns:
        Tuple of (parsed_value, is_valid). If invalid, value is None.
    """
    try:
        value = parse_russian_decimal(text)
        if value is None or value <= 0:
            return None, False
        return value, True
    except (InvalidOperation, ValueError):
        return None, False


def _build_suggested_amount_keyboard(suggested_amount: int) -> ReplyKeyboardMarkup | None:
    """Build a keyboard with suggested amount button if available."""
    if suggested_amount <= 0:
        return None
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text=str(suggested_amount))]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )


def _build_suggested_date_keyboard(suggested_date: date | None) -> ReplyKeyboardMarkup | None:
    """Build a keyboard with suggested date button if available."""
    if not suggested_date:
        return None
    formatted_date = suggested_date.strftime("%d.%m.%Y")
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text=formatted_date)]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )


def _build_suggested_description_keyboard(description: str) -> ReplyKeyboardMarkup:
    """Build a keyboard with suggested description button."""
    return ReplyKeyboardMarkup(
        [[KeyboardButton(text=description)]],
        one_time_keyboard=True,
        resize_keyboard=True,
    )


async def handle_payout_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel/reset payout workflow.

    Clears all conversation context and ends the conversation.
    Called when user types /payout while already in active conversation.
    """
    _clear_payout_context(context)
    return States.END


async def handle_payout_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start payout (transaction) management workflow.

    Admin command to create transactions between accounts.
    Entry point for multi-step conversation.

    Usage: /payout

    Returns:
        Conversation state for next step (SELECT_FROM)
    """
    try:
        if not update.message or not update.message.from_user:
            logger.warning("Received payout command without message or user")
            return States.END

        telegram_id = update.message.from_user.id

        # Verify admin authorization
        admin_user = await verify_bot_admin_authorization(telegram_id)
        if not admin_user:
            logger.warning("Non-admin attempted payout command: telegram_id=%d", telegram_id)
            try:
                await update.message.reply_text(t("err_not_authorized"))
            except Exception:
                pass
            return States.END

        async with AsyncSessionLocal() as session:
            # Get accounts ordered by frequency
            transaction_service = TransactionService(session)
            accounts = await transaction_service.get_accounts_by_from_frequency()

            if not accounts:
                await update.message.reply_text(t("err_no_accounts"))
                return States.END

            # Build inline buttons for account selection
            buttons: list[list[InlineKeyboardButton]] = []
            for account in accounts:
                buttons.append(
                    [
                        InlineKeyboardButton(
                            f"💳 {account.name}", callback_data=f"payout_from:{account.id}"
                        )
                    ]
                )

            keyboard = InlineKeyboardMarkup(buttons)

            await update.message.reply_text(
                t("prompt_select_from_account"),
                reply_markup=keyboard,
            )

            logger.info(
                "Payout workflow started by admin user_id=%d (telegram_id=%d)",
                admin_user.id,
                telegram_id,
            )

            # Store authenticated admin user context
            if context.user_data is not None:
                context.user_data["authorized_admin"] = admin_user
                context.user_data["payout_admin_id"] = telegram_id

            return States.SELECT_FROM

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "starting payout workflow")


async def handle_from_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  # noqa: C901
    """Handle source account selection and show destination accounts.

    User selects source account, then show destination accounts
    ordered by transaction frequency with the selected source.
    """
    try:
        cq = update.callback_query
        if not cq or not cq.data:
            logger.warning("Received from selection callback without data")
            return States.END

        await cq.answer()

        async with AsyncSessionLocal() as session:
            transaction_service = TransactionService(session)

            # Extract account ID
            try:
                from_account_id = int(cq.data.split(":")[1])
            except (IndexError, ValueError):
                logger.warning("Invalid from account callback data: %s", cq.data)
                await cq.edit_message_text(
                    t("err_processing"), reply_markup=InlineKeyboardMarkup([])
                )
                return States.END

            # Fetch and validate from account
            from_account = await transaction_service.get_account_by_id(from_account_id)
            if not from_account:
                logger.warning("From account %d not found", from_account_id)
                await cq.edit_message_text(
                    t("err_processing"), reply_markup=InlineKeyboardMarkup([])
                )
                return States.END

            # Store selected from account
            if context.user_data is not None:
                context.user_data["payout_from_account"] = from_account

            # Get destination accounts ordered by frequency with this source
            to_accounts = await transaction_service.get_accounts_by_to_frequency(from_account_id)

            if not to_accounts:
                await cq.edit_message_text(
                    t("err_no_accounts"), reply_markup=InlineKeyboardMarkup([])
                )
                return States.END

            # Build inline buttons for destination account selection
            buttons: list[list[InlineKeyboardButton]] = []
            for account in to_accounts:
                if account.id != from_account_id:  # Exclude self-transfers
                    buttons.append(
                        [
                            InlineKeyboardButton(
                                f"💳 {account.name}",
                                callback_data=f"payout_to:{account.id}",
                            )
                        ]
                    )

            if not buttons:
                await cq.edit_message_text(
                    t("err_no_destination_accounts"), reply_markup=InlineKeyboardMarkup([])
                )
                return States.END

            keyboard = InlineKeyboardMarkup(buttons)

            await cq.edit_message_text(
                t("prompt_select_to_account"),
                reply_markup=keyboard,
            )

            return States.SELECT_TO

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "handling from selection")


async def handle_to_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:  # noqa: C901
    """Handle destination account selection and calculate suggested amount.

    User selects destination account, calculate suggested amount,
    and prompt for amount input with suggestion.
    """
    try:
        cq = update.callback_query
        if not cq or not cq.data:
            logger.warning("Received to selection callback without data")
            return States.END

        await cq.answer()

        async with AsyncSessionLocal() as session:
            transaction_service = TransactionService(session)

            # Extract account ID
            try:
                to_account_id = int(cq.data.split(":")[1])
            except (IndexError, ValueError):
                logger.warning("Invalid to account callback data: %s", cq.data)
                await cq.edit_message_text(
                    t("err_processing"), reply_markup=InlineKeyboardMarkup([])
                )
                return States.END

            # Fetch and validate to account
            to_account = await transaction_service.get_account_by_id(to_account_id)
            if not to_account:
                logger.warning("To account %d not found", to_account_id)
                await cq.edit_message_text(
                    t("err_processing"), reply_markup=InlineKeyboardMarkup([])
                )
                return States.END

            # Get from account from context
            if context.user_data is None:
                logger.warning("Context user_data is None")
                await cq.edit_message_text(
                    t("err_processing"), reply_markup=InlineKeyboardMarkup([])
                )
                return States.END

            from_account = context.user_data.get("payout_from_account")
            if not from_account:
                logger.warning("From account not found in context")
                await cq.edit_message_text(
                    t("err_processing"), reply_markup=InlineKeyboardMarkup([])
                )
                return States.END

            # Store selected to account
            context.user_data["payout_to_account"] = to_account

            # Calculate suggested amount and current debt
            suggested_amount = await transaction_service.calculate_suggested_amount(
                from_account, to_account
            )

            # Default to showing suggested amount only
            debt_info = t("hint_suggested_amount", amount=format_currency(suggested_amount))

            # For OWNER → ORGANIZATION with positive debt, show both debt and payout
            if (
                from_account.account_type == AccountType.OWNER
                and to_account.account_type == AccountType.ORGANIZATION
            ):
                balance = await transaction_service.balance_service.calculate_account_balance(
                    from_account.id
                )
                if balance > 0:
                    debt_info = t(
                        "hint_debt_and_payout",
                        debt=format_currency(balance),
                        payout=format_currency(suggested_amount),
                    )

            # Build keyboard with suggested amount if available
            keyboard = _build_suggested_amount_keyboard(suggested_amount)

            # Send debt/payout info with optional suggested amount
            if keyboard:
                await cq.edit_message_text(
                    debt_info,
                    reply_markup=InlineKeyboardMarkup([]),
                )
                # Send keyboard in separate message to avoid keyboard state pollution
                from telegram import Message

                if isinstance(cq.message, Message):
                    await cq.message.reply_text(
                        t("prompt_enter_or_use_suggested"),
                        reply_markup=keyboard,
                    )
            else:
                await cq.edit_message_text(
                    debt_info,
                    reply_markup=InlineKeyboardMarkup([]),
                )

            return States.ENTER_AMOUNT

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "handling to selection")


async def handle_amount_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle amount input and generate suggested description.

    User enters transaction amount, validate it, then generate
    suggested description and prompt for confirmation.
    """
    try:
        if not update.message or not update.message.text:
            logger.warning("Received amount input without message or text")
            return States.END

        amount_text = update.message.text.strip()

        # Validate amount
        amount, is_valid = _validate_positive_decimal(amount_text)
        if not is_valid:
            await update.message.reply_text(
                t("err_invalid_amount"),
            )
            return States.ENTER_AMOUNT

        # Get accounts from context
        if context.user_data is None:
            logger.warning("Context user_data is None")
            await update.message.reply_text(t("err_processing"))
            return States.END

        from_account = context.user_data.get("payout_from_account")
        to_account = context.user_data.get("payout_to_account")

        if not from_account or not to_account:
            logger.warning("Accounts not found in context")
            await update.message.reply_text(t("err_processing"))
            return States.END

        # Type narrowing: amount is guaranteed to be Decimal here
        assert amount is not None

        # Store amount
        context.user_data["payout_amount"] = amount

        keyboard = _build_suggested_date_keyboard(date.today())

        await update.message.reply_text(
            t("prompt_enter_transaction_date"),
            reply_markup=keyboard,
        )

        return States.ENTER_TRANSACTION_DATE

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "handling amount input")


async def handle_transaction_date_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle transaction date input and move to description."""
    try:
        if not update.message or not update.message.text:
            logger.warning("Received transaction date input without message or text")
            return States.END

        text = update.message.text.strip()

        try:
            transaction_date = parse_date(text)
            if transaction_date is None:
                raise ValueError("Empty date")
        except ValueError:
            await update.message.reply_text(t("err_invalid_date_format"))
            return States.ENTER_TRANSACTION_DATE

        if context.user_data is None:
            logger.warning("Context user_data is None")
            await update.message.reply_text(t("err_processing"))
            return States.END

        from_account = context.user_data.get("payout_from_account")
        to_account = context.user_data.get("payout_to_account")
        amount = context.user_data.get("payout_amount")

        if not from_account or not to_account or not amount:
            logger.warning("Transaction data not found in context before date input")
            await update.message.reply_text(t("err_processing"))
            return States.END

        context.user_data["payout_date"] = transaction_date

        async with AsyncSessionLocal() as session:
            transaction_service = TransactionService(session)
            suggested_description = transaction_service.generate_description(
                from_account, to_account, amount
            )

            keyboard = _build_suggested_description_keyboard(suggested_description)

            await update.message.reply_text(
                t("prompt_enter_description"),
                reply_markup=keyboard,
            )

            return States.ENTER_DESCRIPTION

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "handling transaction date input")


async def handle_description_input(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle description input and show confirmation summary.

    User enters description, then show confirmation with all details.
    """
    try:
        if not update.message or not update.message.text:
            logger.warning("Received description input without message or text")
            return States.END

        description = update.message.text.strip()

        # Check context data is available
        if context.user_data is None:
            logger.warning("Context user_data is None")
            await update.message.reply_text(t("err_processing"))
            return States.END

        # Store description
        context.user_data["payout_description"] = description

        # Get data from context
        from_account = context.user_data.get("payout_from_account")
        to_account = context.user_data.get("payout_to_account")
        amount = context.user_data.get("payout_amount")
        transaction_date = context.user_data.get("payout_date")

        if not from_account or not to_account or not amount or not transaction_date:
            logger.warning("Transaction data not found in context")
            await update.message.reply_text(t("err_processing"))
            return States.END

        date_text = transaction_date.strftime("%d.%m.%Y")

        # Build confirmation message
        confirmation_text = t(
            "msg_transaction_confirm",
            from_name=from_account.name,
            to_name=to_account.name,
            amount=format_currency(amount),
            description=description,
            date=date_text,
        )

        # Build confirmation buttons
        buttons = [
            [
                InlineKeyboardButton(
                    t("btn_confirm_transaction"), callback_data="payout_confirm:yes"
                )
            ],
            [InlineKeyboardButton(t("btn_cancel"), callback_data="payout_confirm:no")],
        ]
        keyboard = InlineKeyboardMarkup(buttons)

        await update.message.reply_text(
            confirmation_text,
            reply_markup=keyboard,
            parse_mode="HTML",
        )

        return States.CONFIRM

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(e, update, "handling description input")


async def handle_confirm(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle transaction confirmation and create transaction.

    User confirms transaction creation, create the transaction record,
    and reply with result (not edit for forwardability).
    """
    try:
        cq = update.callback_query
        if not cq or not cq.data:
            logger.warning("Received confirm callback without data")
            return States.END

        await cq.answer()

        # Check confirmation response
        action = _parse_confirm_action(cq.data)
        if action != "yes":
            await cq.edit_message_text(
                t("msg_operation_cancelled"), reply_markup=InlineKeyboardMarkup([])
            )
            _clear_payout_context(context)
            return States.END

        payout_data = _get_payout_confirm_data(context)
        if payout_data is None:
            logger.warning("Transaction data incomplete in context")
            await cq.edit_message_text(t("err_processing"), reply_markup=InlineKeyboardMarkup([]))
            return States.END

        from_account, to_account, amount, description, transaction_date, admin_user = payout_data

        try:
            await _create_and_notify_payout_transaction(
                cq=cq,
                context=context,
                from_account=from_account,
                to_account=to_account,
                amount=amount,
                description=description,
                transaction_date=transaction_date,
                admin_user=admin_user,
            )
        finally:
            _clear_payout_context(context)

        return States.END

    except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError, Exception) as e:
        return await handle_conversation_error(
            e, update, "handling confirm", cleanup_fn=_clear_payout_context, context=context
        )


def _parse_confirm_action(callback_data: str) -> str:
    return callback_data.split(":")[1] if ":" in callback_data else ""


def _get_payout_confirm_data(context: ContextTypes.DEFAULT_TYPE):
    if context.user_data is None:
        return None

    from_account = context.user_data.get("payout_from_account")
    to_account = context.user_data.get("payout_to_account")
    amount = context.user_data.get("payout_amount")
    description = context.user_data.get("payout_description")
    transaction_date = context.user_data.get("payout_date")
    admin_user = context.user_data.get("authorized_admin")

    if not all([from_account, to_account, amount, description, transaction_date, admin_user]):
        return None

    return from_account, to_account, amount, description, transaction_date, admin_user


async def _create_and_notify_payout_transaction(
    *,
    cq,
    context: ContextTypes.DEFAULT_TYPE,
    from_account,
    to_account,
    amount,
    description: str,
    transaction_date: date,
    admin_user,
) -> None:
    async with AsyncSessionLocal() as session:
        try:
            transaction_service = TransactionService(session)

            transaction = await transaction_service.create_transaction(
                from_account_id=from_account.id,
                to_account_id=to_account.id,
                amount=amount,
                description=description,
                transaction_date=transaction_date,
                actor_id=admin_user.id,
            )

            await session.commit()

            logger.info(
                "Transaction created by admin user_id=%d: transaction_id=%d",
                admin_user.id,
                transaction.id,
            )

            success_text = t(
                "msg_transaction_created",
                from_name=from_account.name,
                to_name=to_account.name,
                amount=format_currency(amount),
                date=transaction.transaction_date.strftime("%d.%m.%Y"),
                description=description,
            )

            try:
                if context.application:
                    notifier = NotificationService(context.application)
                    await notifier.notify_account_owners_and_representatives(
                        session=session,
                        account_ids=[from_account.id, to_account.id],
                        text=success_text,
                    )
            except Exception:
                logger.exception("Error notifying owners/representatives about payout")

            await cq.edit_message_text(
                success_text,
                reply_markup=InlineKeyboardMarkup([]),
                parse_mode="HTML",
            )

        except (httpx.ConnectError, httpx.TimeoutException, asyncio.TimeoutError) as e:
            logger.error("Network error creating transaction: %s", e, exc_info=True)
            await session.rollback()
            await cq.edit_message_text(
                t("err_network_retry"), reply_markup=InlineKeyboardMarkup([])
            )
        except Exception as e:
            logger.error("Error creating transaction: %s", e, exc_info=True)
            await session.rollback()
            await cq.edit_message_text(t("err_processing"), reply_markup=InlineKeyboardMarkup([]))


__all__ = [
    "States",
    "handle_payout_cancel",
    "handle_payout_command",
    "handle_from_selection",
    "handle_to_selection",
    "handle_amount_input",
    "handle_transaction_date_input",
    "handle_description_input",
    "handle_confirm",
]

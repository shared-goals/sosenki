"""Telegram bot application factory."""

import logging
import warnings

from telegram import Update
from telegram.error import NetworkError, TimedOut
from telegram.ext import (
    Application,
    CallbackQueryHandler,
    CommandHandler,
    ContextTypes,
    ConversationHandler,
    MessageHandler,
    filters,
)
from telegram.request import HTTPXRequest
from telegram.warnings import PTBUserWarning

from src.bot.config import bot_config
from src.bot.handlers.admin_bills import (
    handle_action_selection,
    handle_bills_cancel,
    handle_bills_command,
    handle_budget_conservation_input,
    handle_budget_create_bills,
    handle_budget_main_input,
    handle_electricity_create_bills,
    handle_electricity_losses,
    handle_electricity_meter_end,
    handle_electricity_meter_start,
    handle_electricity_multiplier,
    handle_electricity_rate,
    handle_period_selection,
)
from src.bot.handlers.admin_accounts import (
    States as AccountsStates,
)
from src.bot.handlers.admin_accounts import (
    handle_accounts_action_selection,
    handle_accounts_cancel,
    handle_accounts_command,
    handle_accounts_create_name_input,
    handle_accounts_delete_confirmation,
    handle_accounts_delete_selection,
    handle_accounts_update_name_input,
    handle_accounts_update_selection,
)
from src.bot.handlers.admin_meter import (
    States as MeterStates,
)
from src.bot.handlers.admin_meter import (
    handle_action_selection as handle_meter_action_selection,
)
from src.bot.handlers.admin_meter import (
    handle_date_input,
    handle_delete_confirmation,
    handle_final_confirmation,
    handle_meter_cancel,
    handle_meter_command,
    handle_property_selection,
    handle_show_empty_properties,
    handle_value_input,
)
from src.bot.handlers.admin_payout import (
    States as PayoutStates,
)
from src.bot.handlers.admin_payout import (
    handle_amount_input,
    handle_confirm,
    handle_description_input,
    handle_from_selection,
    handle_payout_cancel,
    handle_payout_command,
    handle_to_selection,
    handle_transaction_date_input,
)
from src.bot.handlers.admin_periods import (
    handle_close_period_confirmation,
    handle_period_action_selection,
    handle_period_months_input,
    handle_period_start_date_input,
    handle_periods_cancel,
    handle_periods_command,
)

# Import from handlers package (modular structure)
from src.bot.handlers.admin_requests import handle_admin_callback, handle_admin_response
from src.bot.handlers.ask import handle_ask_command
from src.bot.handlers.common import handle_request_command, handle_start_command
from src.services.localizer import t

logger = logging.getLogger(__name__)


async def _global_error_handler(update: object, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Safety-net: log all uncaught exceptions and notify user."""
    err = context.error
    is_network = isinstance(err, (TimedOut, NetworkError))
    logger.error("Unhandled %s: %s", type(err).__name__, err, exc_info=err)
    if not isinstance(update, Update):
        return
    msg = t("err_network_retry") if is_network else t("err_processing")
    try:
        if update.effective_message:
            await update.effective_message.reply_text(msg)
    except Exception:
        logger.debug("Could not send error notification to user", exc_info=True)


# Conversation states for service periods
PERIOD_SELECT_ACTION = 10
PERIOD_INPUT_START_DATE = 11
PERIOD_INPUT_MONTHS = 12

# Conversation states for bills workflow (readings/budget/close)
BILLS_SELECT_PERIOD = 1
BILLS_SELECT_ACTION = 2
BILLS_INPUT_METER_START = 3
BILLS_INPUT_METER_END = 4
BILLS_INPUT_MULTIPLIER = 5
BILLS_INPUT_RATE = 6
BILLS_INPUT_LOSSES = 7
BILLS_CONFIRM_ELECTRICITY = 8
BILLS_INPUT_MAIN_BUDGET = 9
BILLS_INPUT_CONSERVATION_BUDGET = 10
BILLS_CONFIRM_BUDGET = 11


async def create_bot_app() -> Application:
    """Create and return Telegram bot application with async handlers.

    T032, T044, T052: Register command handlers with the bot application.
    """
    # Suppress PTBUserWarning about per_message settings in ConversationHandler
    # per_message=False is appropriate when using mixed handler types
    warnings.filterwarnings("ignore", message=".*per_message.*", category=PTBUserWarning)

    builder = Application.builder().token(bot_config.telegram_bot_token)
    if bot_config.all_proxy:
        builder = builder.request(HTTPXRequest(proxy=bot_config.all_proxy))
    app = builder.build()
    app.add_error_handler(_global_error_handler)

    # /start command handler
    app.add_handler(CommandHandler("start", handle_start_command))
    # T031/T032: Register /request command handler
    app.add_handler(CommandHandler("request", handle_request_command))
    # /ask command handler for natural language AI queries
    app.add_handler(CommandHandler("ask", handle_ask_command))
    # Unified admin response handler: handles both Approve and Reject replies
    # Register after /request so it only handles replies to notifications
    # Uses a simple filter: any text message that is a reply (handler will validate content)
    app.add_handler(MessageHandler(filters.TEXT & filters.REPLY, handle_admin_response))

    # Handle inline button callbacks (Approve/Reject) - only catch approve/reject patterns
    # This allows other callbacks (like electricity_bills) to pass through to ConversationHandler
    app.add_handler(CallbackQueryHandler(handle_admin_callback, pattern="^(approve|reject):"))

    # T050: Register bills management command with ConversationHandler
    bills_conv = ConversationHandler(
        entry_points=[CommandHandler("bills", handle_bills_command)],
        states={
            BILLS_SELECT_PERIOD: [
                CallbackQueryHandler(
                    handle_period_selection,
                    pattern="^bill_period:",
                )
            ],
            BILLS_SELECT_ACTION: [
                CallbackQueryHandler(
                    handle_action_selection,
                    pattern="^bill_action:",
                )
            ],
            BILLS_INPUT_METER_START: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_electricity_meter_start)
            ],
            BILLS_INPUT_METER_END: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_electricity_meter_end)
            ],
            BILLS_INPUT_MULTIPLIER: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_electricity_multiplier)
            ],
            BILLS_INPUT_RATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_electricity_rate)
            ],
            BILLS_INPUT_LOSSES: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_electricity_losses)
            ],
            BILLS_CONFIRM_ELECTRICITY: [
                CallbackQueryHandler(
                    handle_electricity_create_bills,
                    pattern="^elec_bills:",
                )
            ],
            BILLS_INPUT_MAIN_BUDGET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_budget_main_input)
            ],
            BILLS_INPUT_CONSERVATION_BUDGET: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_budget_conservation_input)
            ],
            BILLS_CONFIRM_BUDGET: [
                CallbackQueryHandler(
                    handle_budget_create_bills,
                    pattern="^budget_bills:",
                )
            ],
        },
        fallbacks=[CommandHandler("bills", handle_bills_cancel)],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(bills_conv)

    # Register service periods management command with ConversationHandler
    periods_conv = ConversationHandler(
        entry_points=[CommandHandler("periods", handle_periods_command)],
        states={
            PERIOD_SELECT_ACTION: [
                CallbackQueryHandler(
                    handle_period_action_selection,
                    pattern="^period_action:",
                ),
                CallbackQueryHandler(
                    handle_close_period_confirmation,
                    pattern="^close_period:",
                ),
            ],
            PERIOD_INPUT_START_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_period_start_date_input)
            ],
            PERIOD_INPUT_MONTHS: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_period_months_input)
            ],
        },
        fallbacks=[CommandHandler("periods", handle_periods_cancel)],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(periods_conv)

    # Register meter readings management command with ConversationHandler
    meter_conv = ConversationHandler(
        entry_points=[CommandHandler("meter", handle_meter_command)],
        states={
            MeterStates.SELECT_PROPERTY: [
                CallbackQueryHandler(
                    handle_property_selection,
                    pattern="^meter_property_",
                ),
                CallbackQueryHandler(
                    handle_show_empty_properties,
                    pattern="^meter_show_empty$",
                ),
                CallbackQueryHandler(handle_meter_cancel, pattern="^meter_cancel$"),
            ],
            MeterStates.SELECT_ACTION: [
                CallbackQueryHandler(
                    handle_meter_action_selection,
                    pattern="^meter_action_",
                ),
                CallbackQueryHandler(handle_meter_cancel, pattern="^meter_cancel$"),
            ],
            MeterStates.CONFIRM_DELETE: [
                CallbackQueryHandler(
                    handle_delete_confirmation,
                    pattern="^meter_confirm_delete",
                ),
                CallbackQueryHandler(handle_meter_cancel, pattern="^meter_cancel$"),
            ],
            MeterStates.ENTER_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_date_input)
            ],
            MeterStates.ENTER_VALUE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_value_input)
            ],
            MeterStates.CONFIRM: [
                CallbackQueryHandler(
                    handle_final_confirmation,
                    pattern="^meter_confirm_save$",
                ),
                CallbackQueryHandler(handle_meter_cancel, pattern="^meter_cancel$"),
            ],
        },
        fallbacks=[CommandHandler("meter", handle_meter_cancel)],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(meter_conv)

    # Register payout (transaction) management command with ConversationHandler
    payout_conv = ConversationHandler(
        entry_points=[CommandHandler("payout", handle_payout_command)],
        states={
            PayoutStates.SELECT_FROM: [
                CallbackQueryHandler(
                    handle_from_selection,
                    pattern="^payout_from:",
                )
            ],
            PayoutStates.SELECT_TO: [
                CallbackQueryHandler(
                    handle_to_selection,
                    pattern="^payout_to:",
                )
            ],
            PayoutStates.ENTER_AMOUNT: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_amount_input)
            ],
            PayoutStates.ENTER_TRANSACTION_DATE: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_transaction_date_input)
            ],
            PayoutStates.ENTER_DESCRIPTION: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_description_input)
            ],
            PayoutStates.CONFIRM: [
                CallbackQueryHandler(
                    handle_confirm,
                    pattern="^payout_confirm:",
                )
            ],
        },
        fallbacks=[CommandHandler("payout", handle_payout_cancel)],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(payout_conv)

    # Register account management command with ConversationHandler
    accounts_conv = ConversationHandler(
        entry_points=[CommandHandler("accounts", handle_accounts_command)],
        states={
            AccountsStates.SELECT_ACTION: [
                CallbackQueryHandler(
                    handle_accounts_action_selection,
                    pattern="^accounts_action:",
                )
            ],
            AccountsStates.INPUT_CREATE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_accounts_create_name_input)
            ],
            AccountsStates.SELECT_ACCOUNT_FOR_UPDATE: [
                CallbackQueryHandler(
                    handle_accounts_update_selection,
                    pattern="^accounts_update_select:",
                )
            ],
            AccountsStates.INPUT_UPDATE_NAME: [
                MessageHandler(filters.TEXT & ~filters.COMMAND, handle_accounts_update_name_input)
            ],
            AccountsStates.SELECT_ACCOUNT_FOR_DELETE: [
                CallbackQueryHandler(
                    handle_accounts_delete_selection,
                    pattern="^accounts_delete_select:",
                )
            ],
            AccountsStates.CONFIRM_DELETE: [
                CallbackQueryHandler(
                    handle_accounts_delete_confirmation,
                    pattern="^accounts_delete_confirm:",
                )
            ],
        },
        fallbacks=[CommandHandler("accounts", handle_accounts_cancel)],
        allow_reentry=True,
        per_message=False,
    )
    app.add_handler(accounts_conv)

    # Initialize any other bot-level setup here

    return app


__all__ = ["create_bot_app"]

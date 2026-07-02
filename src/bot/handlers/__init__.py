"""Bot handlers package - command, requests, and conversation handlers."""

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
    handle_accounts_action_selection,
    handle_accounts_cancel,
    handle_accounts_command,
    handle_accounts_create_name_input,
    handle_accounts_delete_confirmation,
    handle_accounts_delete_selection,
    handle_accounts_update_name_input,
    handle_accounts_update_selection,
)
from src.bot.handlers.admin_periods import (
    handle_close_period_confirmation,
    handle_period_action_selection,
    handle_period_months_input,
    handle_period_start_date_input,
    handle_periods_command,
)
from src.bot.handlers.admin_requests import handle_admin_callback, handle_admin_response
from src.bot.handlers.common import handle_request_command, handle_start_command

__all__ = [
    # Common handlers
    "handle_start_command",
    "handle_request_command",
    # Admin: Access requests
    "handle_admin_response",
    "handle_admin_callback",
    # Admin: Account management
    "handle_accounts_command",
    "handle_accounts_cancel",
    "handle_accounts_action_selection",
    "handle_accounts_create_name_input",
    "handle_accounts_update_selection",
    "handle_accounts_update_name_input",
    "handle_accounts_delete_selection",
    "handle_accounts_delete_confirmation",
    # Admin: Period management
    "handle_periods_command",
    "handle_period_action_selection",
    "handle_period_start_date_input",
    "handle_period_months_input",
    "handle_close_period_confirmation",
    # Admin: Bills management (readings/budget/close)
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

"""Bot handler for the /debts command.

Lists all owners whose balance is a debt (raw balance > 0), ordered from
largest to smallest, with a grand total. Reuses the same authorization and
balance calculation as the mini-app "accounts" view.
"""

import logging

from sqlalchemy import select
from sqlalchemy.orm import selectinload
from telegram import Update
from telegram.ext import ContextTypes

from src.models.account import Account, AccountType
from src.services import AsyncSessionLocal
from src.services.auth_service import (
    authorize_account_access_for_roles,
    get_authenticated_user,
)
from src.services.balance_service import BalanceCalculationService
from src.services.locale_service import format_currency
from src.services.localizer import t

logger = logging.getLogger(__name__)


async def handle_debts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> None:
    """Handle /debts command.

    Lists all owners with a positive (debt) balance, largest to smallest,
    and shows the grand total.

    Authorization mirrors the mini-app "accounts" view: admins, owners and
    staff are allowed to view all account balances.
    """
    try:
        if not update.message or not update.message.from_user:
            logger.warning("Received /debts without message or user")
            return

        telegram_id = update.message.from_user.id

        async with AsyncSessionLocal() as session:
            # Same authorization as the mini-app accounts view.
            authenticated_user = await get_authenticated_user(session, telegram_id)
            await authorize_account_access_for_roles(
                session,
                authenticated_user,
                1,
                ["is_administrator", "is_owner", "is_staff"],
            )

            stmt = (
                select(Account)
                .where(Account.account_type == AccountType.OWNER)
                .options(selectinload(Account.user))
            )
            result = await session.execute(stmt)
            accounts = result.scalars().all()

            balance_service = BalanceCalculationService(session)
            debts: list[tuple[str, float]] = []
            total = 0.0
            for account in accounts:
                calc = await balance_service.calculate_account_balance_with_display(account.id)
                # Positive raw balance for an owner means they owe a debt.
                if calc.balance > 0:
                    debt = float(calc.balance)
                    total += debt
                    owner_name = account.user.name if account.user else f"account {account.id}"
                    debts.append((owner_name, debt))

            # Largest debt first.
            debts.sort(key=lambda item: item[1], reverse=True)
    except Exception as e:
        logger.error("Error in /debts handler: %s", e, exc_info=True)
        if update.message:
            await update.message.reply_text(t("err_processing"))
        return

    if not debts:
        await update.message.reply_text(t("msg_debts_empty"))
        return

    lines = [t("msg_debts_title"), ""]
    for index, (owner_name, debt) in enumerate(debts, start=1):
        lines.append(f"{index}. {owner_name} — {format_currency(debt)}")
    lines.append("")
    lines.append(t("msg_debts_total", total=format_currency(total)))

    await update.message.reply_text("\n".join(lines))
    logger.info("Sent /debts to %d owners (total %s)", len(debts), format_currency(total))


__all__ = ["handle_debts_command"]

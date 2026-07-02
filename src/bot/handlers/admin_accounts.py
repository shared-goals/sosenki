"""Admin account management handlers with conversation state machine."""

import logging

from sqlalchemy import func, or_, select
from telegram import InlineKeyboardButton, InlineKeyboardMarkup, Update
from telegram.ext import ContextTypes

from src.models.account import Account, AccountType
from src.models.bill import Bill
from src.models.transaction import Transaction
from src.services import AsyncSessionLocal
from src.services.audit_service import AuditService
from src.services.auth_service import verify_bot_admin_authorization
from src.services.localizer import t

logger = logging.getLogger(__name__)


class States:
    """Conversation states for account management workflow."""

    END = -1
    SELECT_ACTION = 1
    INPUT_CREATE_NAME = 2
    SELECT_ACCOUNT_FOR_UPDATE = 3
    INPUT_UPDATE_NAME = 4
    SELECT_ACCOUNT_FOR_DELETE = 5
    CONFIRM_DELETE = 6


_ACCOUNTS_KEYS = [
    "accounts_admin_id",
    "accounts_selected_account_id",
    "accounts_selected_account_name",
    "authorized_admin",
]


def _clear_accounts_context(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Clear all accounts-related context data."""
    if context.user_data is None:
        return
    for key in _ACCOUNTS_KEYS:
        context.user_data.pop(key, None)


def _build_accounts_keyboard(accounts: list[Account], action: str) -> InlineKeyboardMarkup:
    """Build account selection keyboard for update/delete actions."""
    buttons: list[list[InlineKeyboardButton]] = []
    for account in accounts:
        buttons.append(
            [
                InlineKeyboardButton(
                    f"💳 {account.name}",
                    callback_data=f"accounts_{action}:{account.id}",
                )
            ]
        )
    return InlineKeyboardMarkup(buttons)


def _build_actions_keyboard() -> InlineKeyboardMarkup:
    """Build action selection keyboard for /accounts."""
    return InlineKeyboardMarkup(
        [
            [InlineKeyboardButton(t("btn_accounts_list"), callback_data="accounts_action:list")],
            [
                InlineKeyboardButton(
                    t("btn_accounts_create"), callback_data="accounts_action:create"
                )
            ],
            [
                InlineKeyboardButton(
                    t("btn_accounts_update"), callback_data="accounts_action:update"
                )
            ],
            [
                InlineKeyboardButton(
                    t("btn_accounts_delete"), callback_data="accounts_action:delete"
                )
            ],
        ]
    )


async def _get_organization_accounts(session) -> list[Account]:
    """Get organization accounts ordered by name."""
    stmt = (
        select(Account)
        .where(Account.account_type == AccountType.ORGANIZATION)
        .order_by(Account.name.asc())
    )
    result = await session.execute(stmt)
    return list(result.scalars().all())


async def handle_accounts_cancel(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Cancel/reset accounts workflow."""
    _clear_accounts_context(context)
    return States.END


async def handle_accounts_command(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Start account management workflow.

    Admin command to list/create/update/delete organization accounts.
    """
    try:
        if not update.message or not update.message.from_user:
            logger.warning("Received /accounts without message or user")
            return States.END

        telegram_id = update.message.from_user.id
        admin_user = await verify_bot_admin_authorization(telegram_id)
        if not admin_user:
            await update.message.reply_text(t("err_not_authorized"))
            return States.END

        if context.user_data is not None:
            context.user_data["authorized_admin"] = admin_user
            context.user_data["accounts_admin_id"] = telegram_id

        await update.message.reply_text(
            t("prompt_select_accounts_action"),
            reply_markup=_build_actions_keyboard(),
        )

        logger.info(
            "Accounts workflow started by admin user_id=%d (telegram_id=%d)",
            admin_user.id,
            telegram_id,
        )

        return States.SELECT_ACTION

    except Exception as e:
        logger.error("Error in /accounts handler: %s", e, exc_info=True)
        if update.message:
            await update.message.reply_text(t("err_processing"))
        return States.END


async def handle_accounts_action_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle action selection for /accounts workflow."""
    try:
        cq = update.callback_query
        if not cq or not cq.data:
            logger.warning("Received accounts action callback without data")
            return States.END

        await cq.answer()

        action = cq.data.split(":", maxsplit=1)[1]
        async with AsyncSessionLocal() as session:
            if action == "list":
                accounts = await _get_organization_accounts(session)
                if not accounts:
                    await cq.edit_message_text(t("msg_accounts_empty"))
                    return States.END

                lines = [t("msg_accounts_list_title"), ""]
                lines.extend([f"• {account.id}: {account.name}" for account in accounts])
                await cq.edit_message_text("\n".join(lines))
                return States.END

            if action == "create":
                await cq.edit_message_text(t("prompt_accounts_create_name"))
                return States.INPUT_CREATE_NAME

            if action == "update":
                accounts = await _get_organization_accounts(session)
                if not accounts:
                    await cq.edit_message_text(t("msg_accounts_empty"))
                    return States.END

                await cq.edit_message_text(
                    t("prompt_accounts_update_select"),
                    reply_markup=_build_accounts_keyboard(accounts, "update_select"),
                )
                return States.SELECT_ACCOUNT_FOR_UPDATE

            if action == "delete":
                accounts = await _get_organization_accounts(session)
                if not accounts:
                    await cq.edit_message_text(t("msg_accounts_empty"))
                    return States.END

                await cq.edit_message_text(
                    t("prompt_accounts_delete_select"),
                    reply_markup=_build_accounts_keyboard(accounts, "delete_select"),
                )
                return States.SELECT_ACCOUNT_FOR_DELETE

            await cq.edit_message_text(t("err_unknown_action"))
            return States.END

    except Exception as e:
        logger.error("Error in accounts action selection: %s", e, exc_info=True)
        cq = update.callback_query
        if cq and cq.message:
            await cq.message.reply_text(t("err_processing"))
        return States.END


async def handle_accounts_create_name_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle account name input for create action."""
    try:
        if not update.message or not update.message.text:
            return States.END

        account_name = update.message.text.strip()
        if not account_name:
            await update.message.reply_text(t("err_account_name_required"))
            return States.INPUT_CREATE_NAME

        if len(account_name) > 100:
            await update.message.reply_text(t("err_account_name_too_long"))
            return States.INPUT_CREATE_NAME

        admin_user = context.user_data.get("authorized_admin") if context.user_data else None
        if not admin_user:
            await update.message.reply_text(t("err_not_authorized"))
            return States.END

        async with AsyncSessionLocal() as session:
            existing_stmt = select(Account).where(
                Account.name == account_name,
                Account.account_type == AccountType.ORGANIZATION,
            )
            existing_result = await session.execute(existing_stmt)
            existing_account = existing_result.scalar_one_or_none()

            if existing_account:
                await update.message.reply_text(
                    t(
                        "msg_account_exists",
                        account_name=existing_account.name,
                        account_id=existing_account.id,
                    )
                )
                return States.END

            account = Account(name=account_name, account_type=AccountType.ORGANIZATION, user_id=None)
            session.add(account)
            await session.flush()

            await AuditService.log(
                session=session,
                entity_type="account",
                entity_id=account.id,
                action="create",
                actor_id=admin_user.id,
                changes={"name": account.name, "account_type": AccountType.ORGANIZATION.value},
            )
            await session.commit()

            await update.message.reply_text(
                t("msg_account_created", account_name=account.name, account_id=account.id)
            )

        return States.END

    except Exception as e:
        logger.error("Error creating account: %s", e, exc_info=True)
        if update.message:
            await update.message.reply_text(t("err_processing"))
        return States.END


async def handle_accounts_update_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle organization account selection for update action."""
    try:
        cq = update.callback_query
        if not cq or not cq.data:
            return States.END

        await cq.answer()
        account_id = int(cq.data.split(":", maxsplit=1)[1])

        async with AsyncSessionLocal() as session:
            account = await session.get(Account, account_id)
            if not account or account.account_type != AccountType.ORGANIZATION:
                await cq.edit_message_text(t("err_account_not_found"))
                return States.END

            if context.user_data is not None:
                context.user_data["accounts_selected_account_id"] = account.id
                context.user_data["accounts_selected_account_name"] = account.name

            await cq.edit_message_text(t("prompt_accounts_update_name", old_name=account.name))
            return States.INPUT_UPDATE_NAME

    except Exception as e:
        logger.error("Error selecting account for update: %s", e, exc_info=True)
        cq = update.callback_query
        if cq and cq.message:
            await cq.message.reply_text(t("err_processing"))
        return States.END


async def handle_accounts_update_name_input(
    update: Update, context: ContextTypes.DEFAULT_TYPE
) -> int:
    """Handle new account name input for update action."""
    try:
        if not update.message or not update.message.text:
            return States.END

        if context.user_data is None:
            return States.END

        account_id = context.user_data.get("accounts_selected_account_id")
        old_name = context.user_data.get("accounts_selected_account_name")
        if not account_id:
            await update.message.reply_text(t("err_account_not_found"))
            return States.END

        new_name = update.message.text.strip()
        if not new_name:
            await update.message.reply_text(t("err_account_name_required"))
            return States.INPUT_UPDATE_NAME

        if len(new_name) > 100:
            await update.message.reply_text(t("err_account_name_too_long"))
            return States.INPUT_UPDATE_NAME

        admin_user = context.user_data.get("authorized_admin")
        if not admin_user:
            await update.message.reply_text(t("err_not_authorized"))
            return States.END

        async with AsyncSessionLocal() as session:
            account = await session.get(Account, account_id)
            if not account or account.account_type != AccountType.ORGANIZATION:
                await update.message.reply_text(t("err_account_not_found"))
                return States.END

            existing_stmt = select(Account).where(
                Account.name == new_name,
                Account.account_type == AccountType.ORGANIZATION,
                Account.id != account.id,
            )
            existing_result = await session.execute(existing_stmt)
            if existing_result.scalar_one_or_none():
                await update.message.reply_text(t("msg_account_name_taken", account_name=new_name))
                return States.INPUT_UPDATE_NAME

            account.name = new_name
            await session.flush()

            await AuditService.log(
                session=session,
                entity_type="account",
                entity_id=account.id,
                action="update",
                actor_id=admin_user.id,
                changes={"old_name": old_name, "new_name": new_name},
            )

            await session.commit()
            await update.message.reply_text(
                t(
                    "msg_account_updated",
                    account_id=account.id,
                    old_name=old_name,
                    new_name=account.name,
                )
            )

        return States.END

    except Exception as e:
        logger.error("Error updating account: %s", e, exc_info=True)
        if update.message:
            await update.message.reply_text(t("err_processing"))
        return States.END


async def handle_accounts_delete_selection(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle organization account selection for delete action."""
    try:
        cq = update.callback_query
        if not cq or not cq.data:
            return States.END

        await cq.answer()
        account_id = int(cq.data.split(":", maxsplit=1)[1])

        async with AsyncSessionLocal() as session:
            account = await session.get(Account, account_id)
            if not account or account.account_type != AccountType.ORGANIZATION:
                await cq.edit_message_text(t("err_account_not_found"))
                return States.END

            if context.user_data is not None:
                context.user_data["accounts_selected_account_id"] = account.id
                context.user_data["accounts_selected_account_name"] = account.name

            keyboard = InlineKeyboardMarkup(
                [
                    [
                        InlineKeyboardButton(t("btn_yes"), callback_data="accounts_delete_confirm:yes"),
                        InlineKeyboardButton(t("btn_no"), callback_data="accounts_delete_confirm:no"),
                    ]
                ]
            )
            await cq.edit_message_text(
                t("prompt_accounts_delete_confirm", account_name=account.name),
                reply_markup=keyboard,
            )
            return States.CONFIRM_DELETE

    except Exception as e:
        logger.error("Error selecting account for delete: %s", e, exc_info=True)
        cq = update.callback_query
        if cq and cq.message:
            await cq.message.reply_text(t("err_processing"))
        return States.END


async def handle_accounts_delete_confirmation(update: Update, context: ContextTypes.DEFAULT_TYPE) -> int:
    """Handle confirmation for delete action."""
    try:
        cq = update.callback_query
        if not cq or not cq.data:
            return States.END

        await cq.answer()
        decision = cq.data.split(":", maxsplit=1)[1]

        if decision != "yes":
            await cq.edit_message_text(t("msg_operation_cancelled"))
            return States.END

        if context.user_data is None:
            return States.END

        account_id = context.user_data.get("accounts_selected_account_id")
        account_name = context.user_data.get("accounts_selected_account_name")
        admin_user = context.user_data.get("authorized_admin")

        if not account_id or not admin_user:
            await cq.edit_message_text(t("err_processing"))
            return States.END

        async with AsyncSessionLocal() as session:
            account = await session.get(Account, account_id)
            if not account or account.account_type != AccountType.ORGANIZATION:
                await cq.edit_message_text(t("err_account_not_found"))
                return States.END

            tx_count_stmt = select(func.count(Transaction.id)).where(
                or_(Transaction.from_account_id == account_id, Transaction.to_account_id == account_id)
            )
            tx_count_result = await session.execute(tx_count_stmt)
            tx_count = tx_count_result.scalar_one()

            bill_count_stmt = select(func.count(Bill.id)).where(Bill.account_id == account_id)
            bill_count_result = await session.execute(bill_count_stmt)
            bill_count = bill_count_result.scalar_one()

            if tx_count > 0 or bill_count > 0:
                await cq.edit_message_text(
                    t(
                        "err_account_has_operations",
                        tx_count=tx_count,
                        bill_count=bill_count,
                    )
                )
                return States.END

            await session.delete(account)
            await session.flush()

            await AuditService.log(
                session=session,
                entity_type="account",
                entity_id=account_id,
                action="delete",
                actor_id=admin_user.id,
                changes={"name": account_name, "account_type": AccountType.ORGANIZATION.value},
            )

            await session.commit()
            await cq.edit_message_text(
                t("msg_account_deleted", account_name=account_name, account_id=account_id)
            )

        return States.END

    except Exception as e:
        logger.error("Error deleting account: %s", e, exc_info=True)
        cq = update.callback_query
        if cq and cq.message:
            await cq.message.reply_text(t("err_processing"))
        return States.END


__all__ = [
    "States",
    "handle_accounts_command",
    "handle_accounts_cancel",
    "handle_accounts_action_selection",
    "handle_accounts_create_name_input",
    "handle_accounts_update_selection",
    "handle_accounts_update_name_input",
    "handle_accounts_delete_selection",
    "handle_accounts_delete_confirmation",
]
"""Transaction parsing and creation utilities for database seeding.

Unified service for creating account-to-account Transaction records,
replacing dual Debit + Credit logic.
"""

import logging
from decimal import Decimal
from typing import Any, cast

from sqlalchemy.orm import Session

from seeding.core.errors import DataValidationError
from src.models.account import Account, AccountType
from src.models.budget_item import AllocationStrategy, BudgetItem
from src.models.service_period import ServicePeriod
from src.models.transaction import Transaction
from src.models.user import User


def _parse_decimal_field(value: str | None) -> Decimal | None:
    """Parse a string value to Decimal or return None if not provided."""
    return Decimal(str(value)) if value is not None else None


def _update_period_fields(
    period: ServicePeriod,
    electricity_start: str | None,
    electricity_end: str | None,
    electricity_multiplier: str | None,
    electricity_rate: str | None,
    electricity_losses: str | None,
    status: str | None,
    period_months: int | None,
    year_budget: str | None,
    conservation_year_budget: str | None,
) -> None:
    """Update ServicePeriod fields from provided values."""
    elec_start = _parse_decimal_field(electricity_start)
    elec_end = _parse_decimal_field(electricity_end)
    elec_multiplier = _parse_decimal_field(electricity_multiplier)
    elec_rate = _parse_decimal_field(electricity_rate)
    elec_losses = _parse_decimal_field(electricity_losses)
    year_budget_val = _parse_decimal_field(year_budget)
    conservation_year_budget_val = _parse_decimal_field(conservation_year_budget)

    if electricity_start is not None:
        period.electricity_start = elec_start
    if electricity_end is not None:
        period.electricity_end = elec_end
    if electricity_multiplier is not None:
        period.electricity_multiplier = elec_multiplier
    if electricity_rate is not None:
        period.electricity_rate = elec_rate
    if electricity_losses is not None:
        period.electricity_losses = elec_losses
    if status is not None:
        from src.models.service_period import PeriodStatus

        period.status = PeriodStatus(status)
    if period_months is not None:
        period.period_months = period_months
    if year_budget_val is not None:
        period.year_budget = year_budget_val
    if conservation_year_budget_val is not None:
        period.conservation_year_budget = conservation_year_budget_val


def get_or_create_service_period(
    session: Session,
    period_name: str,
    start_date_str: str,
    end_date_str: str,
    electricity_start: str | None = None,
    electricity_end: str | None = None,
    electricity_multiplier: str | None = None,
    electricity_rate: str | None = None,
    electricity_losses: str | None = None,
    status: str | None = None,
    period_months: int | None = None,
    year_budget: str | None = None,
    conservation_year_budget: str | None = None,
) -> ServicePeriod:
    """Get existing service period or create new one.

    Args:
        session: SQLAlchemy session
        period_name: Period name (e.g., "2024-2025")
        start_date_str: Start date as DD.MM.YYYY string
        end_date_str: End date as DD.MM.YYYY string
        electricity_start: Starting meter reading (optional)
        electricity_end: Ending meter reading (optional)
        electricity_multiplier: Consumption multiplier (optional)
        electricity_rate: Rate per kWh (optional)
        electricity_losses: Transmission losses ratio (optional)
        status: Period status - "open" or "closed" (optional, defaults to "open")
        period_months: Duration in months (1-12) (optional)
        year_budget: Annual MAIN bill budget (optional)
        conservation_year_budget: Annual CONSERVATION bill budget (optional)

    Returns:
        ServicePeriod instance

    Raises:
        DataValidationError: On creation failure
    """
    from datetime import datetime as dt

    logger = logging.getLogger("sosenki.seeding.transactions")

    try:
        # Query for existing period
        period = session.query(ServicePeriod).filter(ServicePeriod.name == period_name).first()

        # Parse dates
        start_date = dt.strptime(start_date_str, "%d.%m.%Y").date()
        end_date = dt.strptime(end_date_str, "%d.%m.%Y").date()

        # Parse electricity and budget fields
        elec_start = _parse_decimal_field(electricity_start)
        elec_end = _parse_decimal_field(electricity_end)
        elec_multiplier = _parse_decimal_field(electricity_multiplier)
        elec_rate = _parse_decimal_field(electricity_rate)
        elec_losses = _parse_decimal_field(electricity_losses)
        year_budget_val = _parse_decimal_field(year_budget)
        conservation_year_budget_val = _parse_decimal_field(conservation_year_budget)

        if period:
            logger.debug(f"Found existing service period: {period_name}")
            # Update fields if provided
            _update_period_fields(
                period,
                electricity_start,
                electricity_end,
                electricity_multiplier,
                electricity_rate,
                electricity_losses,
                status,
                period_months,
                year_budget,
                conservation_year_budget,
            )
            return period

        # Create new period
        period = ServicePeriod(
            name=period_name,
            start_date=start_date,
            end_date=end_date,
            electricity_start=elec_start,
            electricity_end=elec_end,
            electricity_multiplier=elec_multiplier,
            electricity_rate=elec_rate,
            electricity_losses=elec_losses,
            status=status or "open",
            period_months=period_months,
            year_budget=year_budget_val,
            conservation_year_budget=conservation_year_budget_val,
        )
        session.add(period)
        session.flush()

        logger.info(f"Created service period: {period_name} ({start_date} - {end_date})")
        return period

    except Exception as e:
        raise DataValidationError(
            f"Failed to get or create service period '{period_name}': {e}"
        ) from e


def get_or_create_budget_item(
    session: Session, budget_item_name: str, service_period: ServicePeriod | None = None
) -> BudgetItem | None:
    """Get existing budget item or create new one.

    Args:
        session: SQLAlchemy session
        budget_item_name: Budget item name (expense type)
        service_period: ServicePeriod (not used, kept for backwards compatibility)

    Returns:
        BudgetItem instance or None if name is empty
    """
    if not budget_item_name:
        return None

    logger = logging.getLogger("sosenki.seeding.transactions")

    try:
        # Query for existing budget item by expense_type only
        budget_item = (
            session.query(BudgetItem).filter(BudgetItem.expense_type == budget_item_name).first()
        )

        if budget_item:
            logger.debug(f"Found existing budget item: {budget_item_name}")
            return budget_item

        # Create new budget item
        budget_item = BudgetItem(
            expense_type=budget_item_name,
            allocation_strategy=AllocationStrategy.NONE,
            year_budget=0,  # Will be calculated/updated separately
        )
        session.add(budget_item)
        session.flush()

        logger.info(f"Created budget item: {budget_item_name}")
        return budget_item
    except Exception as e:
        raise DataValidationError(
            f"Failed to get or create budget item '{budget_item_name}': {e}"
        ) from e


def get_or_create_account(session: Session, name: str) -> Account | None:
    """Get existing organization account or create new one.

    Args:
        session: SQLAlchemy session
        name: Account name (e.g., "Взносы", "Reserve")

    Returns:
        Account instance with account_type='organization', or None if name is 'Skip'

    Raises:
        DataValidationError: On creation failure
    """
    logger = logging.getLogger("sosenki.seeding.transactions")

    # Skip if marked with 'Skip' literal
    if name == "Skip":
        logger.info(f"Skipping account creation: {name}")
        return None

    try:
        # Query for existing account by name
        # (Account names are unique; type is determined by existence of user_id)
        account = session.query(Account).filter(Account.name == name).first()

        if account:
            logger.debug(f"Found existing account: {name}")
            return account

        # Create new organization account
        account = Account(
            name=name,
            account_type=AccountType.ORGANIZATION,
        )
        session.add(account)
        session.flush()

        logger.info(f"Created organization account: {name}")
        return account

    except Exception as e:
        raise DataValidationError(
            f"Failed to get or create organization account '{name}': {e}"
        ) from e


def create_debit_transactions(
    session: Session,
    debit_dicts: list[dict[str, Any]],
    user_map: dict[str, User],
    period: ServicePeriod,
    default_account_name: str = "Взносы",
) -> int:
    """Create debit transactions (user account → organization account).

    Args:
        session: SQLAlchemy session
        debit_dicts: List of dicts with owner_name, amount, debit_date, comment, account_name
        user_map: Dict mapping user names to User instances
        period: ServicePeriod to link transactions to
        default_account_name: Default organization account name

    Returns:
        Number of transactions created

    Raises:
        DataValidationError: On creation failure
    """
    logger = logging.getLogger("sosenki.seeding.transactions")

    try:
        created_count = 0

        for debit_dict in debit_dicts:
            owner_name = debit_dict.pop("owner_name")
            user = user_map.get(owner_name)

            if not user:
                logger.warning(f"Owner not found: {owner_name}, skipping debit")
                continue

            if not user.account:  # type: ignore[has-type]
                logger.warning(f"No personal account for user: {owner_name}, skipping debit")
                continue

            # Get organization account
            account_name = debit_dict.pop("account_name", None) or default_account_name
            community_account = get_or_create_account(session, account_name)

            if not community_account:
                logger.warning(f"Account is Skip marker: {account_name}, skipping debit")
                continue

            # Create transaction: user account → organization account
            user_account = cast(Account, user.account)  # type: ignore[arg-type]
            transaction = Transaction(
                from_account_id=user_account.id,
                to_account_id=community_account.id,
                amount=debit_dict["amount"],
                transaction_date=debit_dict["debit_date"],
                description=debit_dict.get("comment"),
            )
            session.add(transaction)

            logger.info(
                f"Created debit transaction: {owner_name} → {account_name} "
                f"{debit_dict['amount']} {debit_dict['debit_date']}"
            )
            created_count += 1

        logger.info(f"Created {created_count} debit transactions")
        return created_count

    except Exception as e:
        raise DataValidationError(f"Failed to create debit transactions: {e}") from e


def create_credit_transactions(
    session: Session,
    credit_dicts: list[dict[str, Any]],
    user_map: dict[str, User],
    period: ServicePeriod,
    default_account_name: str = "Взносы",
) -> int:
    """Create credit transactions (organization account → user account).

    Args:
        session: SQLAlchemy session
        credit_dicts: List of dicts with payer_name, amount, debit_date, expense_type, description
        user_map: Dict mapping user first names to User instances
        period: ServicePeriod to link transactions to
        default_account_name: Default organization account name

    Returns:
        Number of transactions created

    Raises:
        DataValidationError: On creation failure
    """
    logger = logging.getLogger("sosenki.seeding.transactions")

    try:
        created_count = 0
        community_account = get_or_create_account(session, default_account_name)

        if not community_account:
            raise DataValidationError(f"Default account '{default_account_name}' is marked as Skip")

        for credit_dict in credit_dicts:
            payer_name = credit_dict.pop("payer_name")
            # Extract first name for user lookup
            payer_first_word = payer_name.split()[0] if payer_name else ""
            user = user_map.get(payer_first_word)

            if not user:
                # Check if payer_name is an organization account
                organization_account_for_payer = (
                    session.query(Account)
                    .filter(
                        Account.name == payer_name, Account.account_type == AccountType.ORGANIZATION
                    )
                    .first()
                )

                if organization_account_for_payer:
                    # Use payer as from_account (account-to-account transaction)
                    account_name = credit_dict.pop("account_name", None) or default_account_name
                    to_organization_account = get_or_create_account(session, account_name)

                    if not to_organization_account:
                        logger.warning(
                            f"Target account is Skip marker: {account_name}, skipping credit"
                        )
                        continue

                    budget_item_name = credit_dict.pop("budget_item_name", None)
                    budget_item = get_or_create_budget_item(session, budget_item_name, period)

                    transaction = Transaction(
                        from_account_id=organization_account_for_payer.id,
                        to_account_id=to_organization_account.id,
                        amount=credit_dict["amount"],
                        transaction_date=credit_dict["debit_date"],
                        budget_item_id=budget_item.id if budget_item else None,
                        description=(
                            f"{credit_dict['expense_type']}: {credit_dict.get('description', '')}"
                            if credit_dict.get("expense_type")
                            else credit_dict.get("description", "")
                        ).strip(),
                    )
                    session.add(transaction)

                    logger.info(
                        f"Created credit transaction: {payer_name} → {to_organization_account.name} "
                        f"{credit_dict['amount']} {credit_dict['debit_date']} "
                        f"({credit_dict['expense_type']})"
                    )
                    created_count += 1
                    continue

                logger.warning(f"Payer not found: {payer_name}, skipping credit")
                continue

            if not user.account:  # type: ignore[has-type]
                logger.warning(f"No personal account for user: {payer_name}, skipping credit")
                continue

            # Get or use account name from parsed data
            account_name = credit_dict.pop("account_name", None) or default_account_name
            organization_account = get_or_create_account(session, account_name)

            if not organization_account:
                logger.warning(f"Account is Skip marker: {account_name}, skipping credit")
                continue

            # Get or create budget item if specified
            budget_item_name = credit_dict.pop("budget_item_name", None)
            budget_item = get_or_create_budget_item(session, budget_item_name, period)

            # Create transaction: user account → organization account
            # (user contributes via this credit)
            user_account = cast(Account, user.account)  # type: ignore[arg-type]
            transaction = Transaction(
                from_account_id=user_account.id,
                to_account_id=organization_account.id,
                amount=credit_dict["amount"],
                transaction_date=credit_dict["debit_date"],
                budget_item_id=budget_item.id if budget_item else None,
                description=(
                    f"{credit_dict['expense_type']}: {credit_dict.get('description', '')}"
                    if credit_dict.get("expense_type")
                    else credit_dict.get("description", "")
                ).strip(),
            )
            session.add(transaction)

            logger.info(
                f"Created credit transaction: {payer_name} → {organization_account.name} "
                f"{credit_dict['amount']} {credit_dict['debit_date']} "
                f"({credit_dict['expense_type']})"
            )
            created_count += 1

        logger.info(f"Created {created_count} credit transactions")
        return created_count

    except Exception as e:
        raise DataValidationError(f"Failed to create credit transactions: {e}") from e


__all__ = [
    "get_or_create_service_period",
    "get_or_create_account",
    "get_or_create_budget_item",
    "create_debit_transactions",
    "create_credit_transactions",
]

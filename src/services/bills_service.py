"""Unified service for all bill calculations and database operations."""

import logging
from datetime import date
from decimal import ROUND_HALF_UP, Decimal
from typing import NamedTuple

from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.account import Account
from src.models.bill import Bill, BillType
from src.models.electricity_reading import ElectricityReading
from src.models.property import Property
from src.models.service_period import ServicePeriod
from src.models.user import User
from src.services.audit_service import AuditService

logger = logging.getLogger(__name__)


class OwnerShare(NamedTuple):
    """Unified owner share information for bill calculations.

    Contains all fields needed for displaying bills with percentages and usernames.
    """

    user_id: int
    user_name: str
    total_share_weight: Decimal
    calculated_bill_amount: Decimal


class PersonalElectricityBill(NamedTuple):
    """Personal electricity bill preview for a single property."""

    owner_id: int
    owner_name: str
    property_id: int
    property_name: str
    start_reading_date: date
    start_reading_value: Decimal
    end_reading_date: date
    end_reading_value: Decimal
    consumption_kwh: Decimal
    bill_amount: Decimal


class BillsService:
    """Async service for bill database operations.

    Encapsulates all Bill CRUD operations and bill amount calculations.
    Used by admin handlers, MCP server, and API endpoints.
    """

    def __init__(self, session: AsyncSession):
        """Initialize with async database session."""
        self.session = session

    async def create_shared_electricity_bills(
        self,
        period_id: int,
        owner_shares: list,
        actor_id: int | None = None,
    ) -> int:
        """Create SHARED_ELECTRICITY bills for each owner.

        Looks up owner's account and creates Bill record.

        Args:
            period_id: Service period ID
            owner_shares: List of OwnerShare namedtuples with user_id and calculated_bill_amount
            actor_id: Admin user ID who created bills (optional)

        Returns:
            Count of bills created
        """
        bills_created = await self._add_shared_electricity_bills(
            period_id=period_id,
            owner_shares=owner_shares,
            actor_id=actor_id,
        )

        await self.session.commit()

        logger.info(
            "Created %d shared electricity bills for period %d",
            bills_created,
            period_id,
        )

        return bills_created

    async def count_electricity_bills_for_period(self, service_period_id: int) -> int:
        """Count any electricity-related bills for the given period.

        Includes both personal (ELECTRICITY) and shared (SHARED_ELECTRICITY) bills.
        """
        result = await self.session.execute(
            select(func.count(Bill.id)).where(
                (Bill.service_period_id == service_period_id)
                & (Bill.bill_type.in_([BillType.ELECTRICITY, BillType.SHARED_ELECTRICITY]))
            )
        )
        return int(result.scalar() or 0)

    async def count_budget_bills_for_period(self, service_period_id: int) -> int:
        """Count any budget-related bills for the given period.

        Includes both MAIN and CONSERVATION bills.
        """
        result = await self.session.execute(
            select(func.count(Bill.id)).where(
                (Bill.service_period_id == service_period_id)
                & (Bill.bill_type.in_([BillType.MAIN, BillType.CONSERVATION]))
            )
        )
        return int(result.scalar() or 0)

    async def calculate_personal_electricity_bills_from_readings(
        self,
        *,
        service_period: ServicePeriod,
        electricity_rate: Decimal,
    ) -> tuple[list[PersonalElectricityBill], Decimal]:
        """Calculate personal electricity bills per property based on readings.

        For each active property:
        - start reading: latest reading with date <= service_period.start_date
        - end reading: latest reading with date <= service_period.end_date
        - amount = (end - start) * electricity_rate

        Skips properties with missing start/end readings.

        Raises ValueError if any readings are inconsistent (end < start).
        """
        if electricity_rate <= 0:
            raise ValueError("Electricity rate must be positive")

        stmt = (
            select(
                Property.id,
                Property.property_name,
                Property.owner_id,
                User.name,
            )
            .join(User, User.id == Property.owner_id)
            .where(Property.is_active == True)  # noqa: E712
            .order_by(User.name.asc(), Property.property_name.asc())
        )
        result = await self.session.execute(stmt)
        properties = result.all()

        property_ids = [row[0] for row in properties]

        from src.services.electricity_reading_service import ElectricityReadingService

        reading_service = ElectricityReadingService(self.session)
        start_by_property = await reading_service.get_latest_readings_for_properties_at_or_before(
            property_ids,
            service_period.start_date,
        )
        end_by_property = await reading_service.get_latest_readings_for_properties_at_or_before(
            property_ids,
            service_period.end_date,
        )

        inconsistent: list[str] = []
        bills: list[PersonalElectricityBill] = []
        total = Decimal("0")

        for property_id, property_name, owner_id, owner_name in properties:
            start_reading = start_by_property.get(property_id)
            end_reading = end_by_property.get(property_id)

            if not start_reading or not end_reading:
                continue

            # Type narrowing
            assert isinstance(start_reading, ElectricityReading)
            assert isinstance(end_reading, ElectricityReading)

            consumption = end_reading.reading_value - start_reading.reading_value
            if consumption < 0:
                inconsistent.append(
                    f"{property_name} ({start_reading.reading_value} → {end_reading.reading_value})"
                )
                continue

            if consumption == 0:
                continue

            amount = (consumption * electricity_rate).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

            bills.append(
                PersonalElectricityBill(
                    owner_id=owner_id,
                    owner_name=owner_name or f"User {owner_id}",
                    property_id=property_id,
                    property_name=property_name,
                    start_reading_date=start_reading.reading_date,
                    start_reading_value=start_reading.reading_value,
                    end_reading_date=end_reading.reading_date,
                    end_reading_value=end_reading.reading_value,
                    consumption_kwh=consumption,
                    bill_amount=amount,
                )
            )
            total += amount

        if inconsistent:
            raise ValueError("INCONSISTENT_READINGS:" + "; ".join(inconsistent))

        return bills, total

    async def create_personal_electricity_bills(
        self,
        *,
        period_id: int,
        personal_bills: list[PersonalElectricityBill],
        actor_id: int | None = None,
    ) -> int:
        """Create personal ELECTRICITY bills for each property."""
        bills_created = await self._add_personal_electricity_bills(
            period_id=period_id,
            personal_bills=personal_bills,
            actor_id=actor_id,
        )
        await self.session.commit()
        return bills_created

    async def create_personal_and_shared_electricity_bills(
        self,
        *,
        period_id: int,
        personal_bills: list[PersonalElectricityBill],
        owner_shares: list[OwnerShare],
        actor_id: int | None = None,
    ) -> tuple[int, int]:
        """Create both personal and shared electricity bills in one commit.

        Fails if any electricity bills already exist for the period.
        """
        existing_count = await self.count_electricity_bills_for_period(period_id)
        if existing_count > 0:
            raise ValueError(f"Electricity bills already exist for period {period_id}")

        personal_count = await self._add_personal_electricity_bills(
            period_id=period_id,
            personal_bills=personal_bills,
            actor_id=actor_id,
        )
        shared_count = await self._add_shared_electricity_bills(
            period_id=period_id,
            owner_shares=owner_shares,
            actor_id=actor_id,
        )

        await self.session.commit()
        return personal_count, shared_count

    async def _add_shared_electricity_bills(
        self,
        *,
        period_id: int,
        owner_shares: list[OwnerShare],
        actor_id: int | None,
    ) -> int:
        bills_created = 0

        for share in owner_shares:
            stmt = select(Account).filter(
                Account.user_id == share.user_id,
                Account.account_type == "owner",
            )
            result = await self.session.execute(stmt)
            account = result.scalar_one_or_none()

            if not account:
                continue

            bill = Bill(
                service_period_id=period_id,
                account_id=account.id,
                property_id=None,
                bill_type=BillType.SHARED_ELECTRICITY,
                bill_amount=share.calculated_bill_amount,
            )
            self.session.add(bill)
            await self.session.flush()

            await AuditService.log(
                session=self.session,
                entity_type="bill",
                entity_id=bill.id,
                action="create",
                actor_id=actor_id,
                changes={
                    "bill_type": "shared_electricity",
                    "account_id": account.id,
                    "account_name": account.name,
                    "period_id": period_id,
                    "amount": float(share.calculated_bill_amount),
                },
            )
            bills_created += 1

        return bills_created

    async def _add_personal_electricity_bills(
        self,
        *,
        period_id: int,
        personal_bills: list[PersonalElectricityBill],
        actor_id: int | None,
    ) -> int:
        bills_created = 0

        for personal in personal_bills:
            stmt = select(Account).filter(
                Account.user_id == personal.owner_id,
                Account.account_type == "owner",
            )
            result = await self.session.execute(stmt)
            account = result.scalar_one_or_none()

            if not account:
                continue

            bill = Bill(
                service_period_id=period_id,
                account_id=account.id,
                property_id=personal.property_id,
                bill_type=BillType.ELECTRICITY,
                bill_amount=personal.bill_amount,
            )
            self.session.add(bill)
            await self.session.flush()

            await AuditService.log(
                session=self.session,
                entity_type="bill",
                entity_id=bill.id,
                action="create",
                actor_id=actor_id,
                changes={
                    "bill_type": "electricity",
                    "account_id": account.id,
                    "account_name": account.name,
                    "property_id": personal.property_id,
                    "property_name": personal.property_name,
                    "period_id": period_id,
                    "amount": float(personal.bill_amount),
                    "start_reading_date": personal.start_reading_date.isoformat(),
                    "start_reading_value": str(personal.start_reading_value),
                    "end_reading_date": personal.end_reading_date.isoformat(),
                    "end_reading_value": str(personal.end_reading_value),
                },
            )
            bills_created += 1

        return bills_created

    async def calculate_main_bills(
        self, year_budget: Decimal, period_months: int
    ) -> list[tuple[int, Decimal]]:
        """Calculate MAIN bills for ALL active properties.

        MAIN bills are paid by ALL active owners based on sum of share_weights
        of ALL their properties (including conservation properties).

        Formula: (year_budget / 12 * period_months / 12) * (share_weight / 100)
        Result is grouped by owner (sum across owner's properties).

        Args:
            year_budget: Total annual budget for MAIN bills
            period_months: Number of months in period (1-12)

        Returns:
            List of (user_id, calculated_amount) tuples
        """
        if year_budget <= 0 or period_months < 1 or period_months > 12:
            return []

        # Fetch ALL active properties (including conservation) with owners
        stmt = (
            select(Property.owner_id, Property.share_weight)
            .filter(Property.is_active)
            .where(Property.share_weight.isnot(None))
        )
        result = await self.session.execute(stmt)
        properties = result.all()

        if not properties:
            return []

        # Calculate amounts per property, group by owner
        owner_totals: dict[int, Decimal] = {}
        monthly_budget = year_budget / Decimal(12)

        for owner_id, share_weight in properties:
            amount = monthly_budget * Decimal(period_months) * (share_weight / Decimal(100))
            owner_totals[owner_id] = owner_totals.get(owner_id, Decimal(0)) + amount

        return list(owner_totals.items())

    async def calculate_conservation_bills(
        self, conservation_year_budget: Decimal, period_months: int
    ) -> list[tuple[int, Decimal]]:
        """Calculate CONSERVATION bills for conservation-flagged properties.

        CONSERVATION bills are ADDITIONAL charges only for owners with conservation properties.
        Sum of conservation property share_weights is normalized to 100%,
        then coefficient is applied for proportional distribution.

        Formula:
        - coefficient = 100 / sum(share_weights for is_conservation=true)
        - amount_per_property = (conservation_year_budget / 12 * period_months / 12)
                              * (share_weight / 100 * coefficient)
        - Result grouped by owner (sum across owner's properties)

        Args:
            conservation_year_budget: Total annual budget for CONSERVATION bills
            period_months: Number of months in period (1-12)

        Returns:
            List of (user_id, calculated_amount) tuples
        """
        if conservation_year_budget <= 0 or period_months < 1 or period_months > 12:
            return []

        # Fetch all active conservation properties with owners
        stmt = (
            select(Property.owner_id, Property.share_weight)
            .filter(Property.is_active, Property.is_conservation)
            .where(Property.share_weight.isnot(None))
        )
        result = await self.session.execute(stmt)
        properties = result.all()

        if not properties:
            return []

        # Calculate total share weight sum
        total_share_weight = sum(weight for _, weight in properties)

        if total_share_weight <= 0:
            return []

        # Calculate coefficient to normalize to 100%
        coefficient = Decimal(100) / total_share_weight

        # Calculate amounts per property, group by owner
        owner_totals: dict[int, Decimal] = {}
        monthly_budget = conservation_year_budget / Decimal(12)

        for owner_id, share_weight in properties:
            normalized_weight = share_weight * coefficient
            amount = monthly_budget * Decimal(period_months) * (normalized_weight / Decimal(100))
            owner_totals[owner_id] = owner_totals.get(owner_id, Decimal(0)) + amount

        return list(owner_totals.items())

    async def create_main_bills(
        self,
        period_id: int,
        calculations: list[tuple[int, Decimal]] | list[OwnerShare],
        actor_id: int | None = None,
    ) -> int:
        """Create MAIN bills for calculated owner amounts.

        Args:
            period_id: Service period ID
            calculations: List of (user_id, amount) tuples from calculate_main_bills() or OwnerShare objects
            actor_id: Admin user ID performing the action (for audit)

        Returns:
            Number of bills created
        """
        existing_count = await self.session.execute(
            select(func.count(Bill.id)).where(
                (Bill.service_period_id == period_id) & (Bill.bill_type == BillType.MAIN)
            )
        )
        if int(existing_count.scalar() or 0) > 0:
            raise ValueError(f"MAIN bills already exist for period {period_id}")

        bills_created = 0

        for calculation in calculations:
            # Support both tuple and OwnerShare formats
            if isinstance(calculation, OwnerShare):
                user_id = calculation.user_id
                amount = calculation.calculated_bill_amount
            else:
                user_id, amount = calculation
            # Find owner account for this user
            stmt = select(Account).filter(
                Account.user_id == user_id,
                Account.account_type == "owner",
            )
            result = await self.session.execute(stmt)
            account = result.scalar_one_or_none()

            if account:
                bill = Bill(
                    service_period_id=period_id,
                    account_id=account.id,
                    property_id=None,
                    bill_type=BillType.MAIN,
                    bill_amount=amount,
                )
                self.session.add(bill)
                await self.session.flush()  # Get bill ID

                # Audit log
                await AuditService.log(
                    session=self.session,
                    entity_type="bill",
                    entity_id=bill.id,
                    action="create",
                    actor_id=actor_id,
                    changes={
                        "bill_type": "main",
                        "account_id": account.id,
                        "account_name": account.name,
                        "period_id": period_id,
                        "amount": float(amount),
                    },
                )
                bills_created += 1

        await self.session.commit()

        logger.info(
            "Created %d MAIN bills for period %d (actor_id=%s)",
            bills_created,
            period_id,
            actor_id,
        )

        return bills_created

    async def create_conservation_bills(
        self,
        period_id: int,
        calculations: list[tuple[int, Decimal]] | list[OwnerShare],
        actor_id: int | None = None,
    ) -> int:
        """Create CONSERVATION bills for calculated owner amounts.

        Args:
            period_id: Service period ID
            calculations: List of (user_id, amount) tuples from calculate_conservation_bills() or OwnerShare objects
            actor_id: Admin user ID performing the action (for audit)

        Returns:
            Number of bills created
        """
        existing_count = await self.session.execute(
            select(func.count(Bill.id)).where(
                (Bill.service_period_id == period_id) & (Bill.bill_type == BillType.CONSERVATION)
            )
        )
        if int(existing_count.scalar() or 0) > 0:
            raise ValueError(f"CONSERVATION bills already exist for period {period_id}")

        bills_created = 0

        for calculation in calculations:
            # Support both tuple and OwnerShare formats
            if isinstance(calculation, OwnerShare):
                user_id = calculation.user_id
                amount = calculation.calculated_bill_amount
            else:
                user_id, amount = calculation
            # Find owner account for this user
            stmt = select(Account).filter(
                Account.user_id == user_id,
                Account.account_type == "owner",
            )
            result = await self.session.execute(stmt)
            account = result.scalar_one_or_none()

            if account:
                bill = Bill(
                    service_period_id=period_id,
                    account_id=account.id,
                    property_id=None,
                    bill_type=BillType.CONSERVATION,
                    bill_amount=amount,
                )
                self.session.add(bill)
                await self.session.flush()  # Get bill ID

                # Audit log
                await AuditService.log(
                    session=self.session,
                    entity_type="bill",
                    entity_id=bill.id,
                    action="create",
                    actor_id=actor_id,
                    changes={
                        "bill_type": "conservation",
                        "account_id": account.id,
                        "account_name": account.name,
                        "period_id": period_id,
                        "amount": float(amount),
                    },
                )
                bills_created += 1

        await self.session.commit()

        logger.info(
            "Created %d CONSERVATION bills for period %d (actor_id=%s)",
            bills_created,
            period_id,
            actor_id,
        )

        return bills_created

    # === Electricity billing methods (merged from ElectricityService) ===

    @staticmethod
    def calculate_total_electricity(
        electricity_start: Decimal,
        electricity_end: Decimal,
        electricity_multiplier: Decimal,
        electricity_rate: Decimal,
        electricity_losses: Decimal,
    ) -> Decimal:
        """Calculate total electricity cost.

        Formula: (end - start) × multiplier × rate × (1 + losses)

        This is a pure calculation method - no DB access required.

        Args:
            electricity_start: Starting meter reading (kWh)
            electricity_end: Ending meter reading (kWh)
            electricity_multiplier: Consumption multiplier
            electricity_rate: Rate per kWh
            electricity_losses: Transmission losses ratio (e.g., 0.2 for 20%)

        Returns:
            Total electricity cost as Decimal (rounded to 2 decimal places)

        Raises:
            ValueError: If electricity_end <= electricity_start or any negative values
        """
        if electricity_end <= electricity_start:
            raise ValueError("Electricity end reading must be greater than start reading")
        if electricity_start < 0 or electricity_end < 0:
            raise ValueError("Electricity readings cannot be negative")
        if electricity_multiplier <= 0 or electricity_rate <= 0:
            raise ValueError("Multiplier and rate must be positive")
        if electricity_losses < 0:
            raise ValueError("Losses cannot be negative")

        consumption = electricity_end - electricity_start
        total = (
            consumption
            * electricity_multiplier
            * electricity_rate
            * (Decimal(1) + electricity_losses)
        )

        return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)

    async def get_electricity_bills_for_period(
        self,
        service_period_id: int,
    ) -> Decimal:
        """Query sum of all existing ELECTRICITY bills for a service period.

        Args:
            service_period_id: Service period ID to query

        Returns:
            Sum of all ELECTRICITY bill amounts for the period (or 0 if none exist)
        """
        result = await self.session.execute(
            select(func.sum(Bill.bill_amount)).where(
                (Bill.service_period_id == service_period_id)
                & (Bill.bill_type == BillType.ELECTRICITY)
            )
        )

        return result.scalar() or Decimal(0)

    async def calculate_owner_shares(
        self,
        service_period: ServicePeriod,
    ) -> dict[int, Decimal]:
        """Aggregate properties by owner with sum of share_weight.

        Args:
            service_period: Service period to query properties for

        Returns:
            Dict mapping user_id → total_share_weight for all property owners
        """
        # Query all active properties and group by owner
        stmt = (
            select(
                Property.owner_id,
                func.sum(Property.share_weight).label("total_weight"),
            )
            .where(
                Property.is_active == True  # noqa: E712
            )
            .group_by(Property.owner_id)
        )

        result = await self.session.execute(stmt)
        rows = result.all()

        owner_shares = {}
        for owner_id, total_weight in rows:
            if total_weight is not None:
                owner_shares[owner_id] = Decimal(str(total_weight))

        return owner_shares

    async def distribute_shared_costs(
        self,
        total_shared_cost: Decimal,
        service_period: ServicePeriod,
    ) -> list[OwnerShare]:
        """Calculate proportional distribution of shared electricity costs.

        Distribution formula: user_share = total × (user_weight_sum / total_weight_sum)

        Args:
            total_shared_cost: Total shared electricity cost to distribute (>= 0)
            service_period: Service period for context

        Returns:
            List of OwnerShare tuples with calculated amounts per owner
        """
        if total_shared_cost < 0:
            raise ValueError("Total shared cost cannot be negative")

        # Get owner shares (weight aggregation)
        owner_shares = await self.calculate_owner_shares(service_period)

        if not owner_shares:
            logger.warning("No active properties found for distribution")
            return []

        # Calculate total weight sum
        total_weight_sum = sum(owner_shares.values())

        if total_weight_sum == 0:
            logger.warning("Total weight sum is zero, cannot distribute")
            return []

        # Calculate per-owner shares
        result = []
        for owner_id, owner_weight in owner_shares.items():
            # Get user name
            stmt = select(User).where(User.id == owner_id)
            user_result = await self.session.execute(stmt)
            user = user_result.scalar_one_or_none()
            if not user:
                logger.warning("User %d not found for owner_id", owner_id)
                continue

            # Calculate proportional share
            user_share_amount = (total_shared_cost * (owner_weight / total_weight_sum)).quantize(
                Decimal("0.01"), rounding=ROUND_HALF_UP
            )

            result.append(
                OwnerShare(
                    user_id=owner_id,
                    user_name=user.name or f"User {owner_id}",
                    total_share_weight=owner_weight,
                    calculated_bill_amount=user_share_amount,
                )
            )

        return result

    async def get_previous_service_period(self) -> ServicePeriod | None:
        """Get the most recent open service period.

        Used for fetching default electricity parameters.

        Returns:
            Previous service period or None if not found
        """
        stmt = (
            select(ServicePeriod)
            .where(ServicePeriod.status == "open")
            .order_by(ServicePeriod.start_date.desc())
            .limit(1)
        )

        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()


__all__ = ["BillsService", "OwnerShare", "PersonalElectricityBill"]

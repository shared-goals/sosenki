"""Service period management service for database operations."""

import logging
from dataclasses import dataclass
from datetime import date
from decimal import Decimal
from typing import NamedTuple

from dateutil.relativedelta import relativedelta
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.service_period import ServicePeriod
from src.services.audit_service import AuditService

logger = logging.getLogger(__name__)


@dataclass
class PeriodDefaults:
    """Previous period values for form defaults."""

    electricity_end: str | None = None
    electricity_multiplier: str | None = None
    electricity_rate: str | None = None
    electricity_losses: str | None = None


class PeriodInfo(NamedTuple):
    """Period information for API/MCP responses."""

    period_id: int
    name: str
    start_date: str  # ISO format
    end_date: str  # ISO format
    is_active: bool
    status: str


class ServicePeriodService:
    """Async service for service period database operations.

    Encapsulates all ServicePeriod and related Bill CRUD operations.
    Used by bot handlers, MCP server, and API endpoints.
    """

    def __init__(self, session: AsyncSession):
        """Initialize with async database session."""
        self.session = session

    async def get_open_periods(self) -> list[ServicePeriod]:
        """Get all open service periods ordered by start_date desc.

        Returns:
            List of all open ServicePeriod objects
        """
        stmt = (
            select(ServicePeriod)
            .filter(ServicePeriod.status == "open")
            .order_by(ServicePeriod.start_date.desc())
        )
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def get_by_id(self, period_id: int) -> ServicePeriod | None:
        """Get service period by ID.

        Args:
            period_id: Period ID to fetch

        Returns:
            ServicePeriod if found, None otherwise
        """
        return await self.session.get(ServicePeriod, period_id)

    async def get_period_info(self, period_id: int) -> PeriodInfo | None:
        """Get period info with active status calculation.

        Args:
            period_id: Period ID to fetch

        Returns:
            PeriodInfo if found, None otherwise
        """
        period = await self.get_by_id(period_id)
        if not period:
            return None

        today = date.today()
        is_active = period.start_date <= today <= period.end_date

        return PeriodInfo(
            period_id=period.id,
            name=period.name,
            start_date=period.start_date.isoformat(),
            end_date=period.end_date.isoformat(),
            is_active=is_active,
            status=period.status or "unknown",
        )

    async def get_latest_period(self) -> ServicePeriod | None:
        """Get most recent period by end_date.

        Used for suggesting default start date for new periods.

        Returns:
            Most recent ServicePeriod or None if no periods exist
        """
        stmt = select(ServicePeriod).order_by(ServicePeriod.end_date.desc()).limit(1)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_previous_period(self, current_start_date: date) -> ServicePeriod | None:
        """Get period where end_date equals given start_date.

        Used for fetching default electricity values from previous period.
        If multiple periods match, prefer one with populated electricity defaults.

        Args:
            current_start_date: Start date of current period

        Returns:
            Previous ServicePeriod or None if not found
        """
        stmt = (
            select(ServicePeriod)
            .filter(ServicePeriod.end_date == current_start_date)
            # Prefer rows that have electricity defaults filled.
            .order_by(
                ServicePeriod.electricity_end.is_(None).asc(),
                ServicePeriod.electricity_rate.is_(None).asc(),
                ServicePeriod.electricity_multiplier.is_(None).asc(),
                ServicePeriod.electricity_losses.is_(None).asc(),
                ServicePeriod.start_date.desc(),
                ServicePeriod.id.desc(),
            )
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_previous_period_defaults(self, current_start_date: date) -> PeriodDefaults:
        """Get electricity defaults from previous period.

        Args:
            current_start_date: Start date of current period

        Returns:
            PeriodDefaults with previous period electricity values
        """
        previous_period = await self.get_previous_period(current_start_date)
        if not previous_period:
            return PeriodDefaults()

        return PeriodDefaults(
            electricity_end=(
                str(previous_period.electricity_end) if previous_period.electricity_end else None
            ),
            electricity_multiplier=(
                str(previous_period.electricity_multiplier)
                if previous_period.electricity_multiplier
                else None
            ),
            electricity_rate=(
                str(previous_period.electricity_rate) if previous_period.electricity_rate else None
            ),
            electricity_losses=(
                str(previous_period.electricity_losses)
                if previous_period.electricity_losses
                else None
            ),
        )

    async def list_periods(self, limit: int = 10) -> list[ServicePeriod]:
        """List all periods ordered by start_date desc.

        Args:
            limit: Maximum number of periods to return

        Returns:
            List of ServicePeriod objects
        """
        stmt = select(ServicePeriod).order_by(ServicePeriod.start_date.desc()).limit(limit)
        result = await self.session.execute(stmt)
        return list(result.scalars().all())

    async def list_periods_info(self, limit: int = 10) -> list[PeriodInfo]:
        """List periods as PeriodInfo objects.

        Args:
            limit: Maximum number of periods to return

        Returns:
            List of PeriodInfo objects
        """
        periods = await self.list_periods(limit)
        today = date.today()
        return [
            PeriodInfo(
                period_id=p.id,
                name=p.name,
                start_date=p.start_date.isoformat(),
                end_date=p.end_date.isoformat(),
                is_active=p.start_date <= today <= p.end_date,
                status=p.status or "unknown",
            )
            for p in periods
        ]

    async def create_period(
        self,
        start_date: date,
        period_months: int = 1,
        name: str | None = None,
        actor_id: int | None = None,
    ) -> ServicePeriod:
        """Create new service period.

        Args:
            start_date: Period start date (must be first day of month)
            period_months: Number of months in period (default 1, valid range 1-12)
            name: Optional custom name (auto-generated if not provided)
            actor_id: Admin user ID who created the period (optional, for audit)

        Returns:
            Created ServicePeriod object

        Raises:
            ValueError: If start_date is not first day of month or period_months out of range
        """
        # Validate start_date is first day of month
        if start_date.day != 1:
            raise ValueError("start_date must be the first day of the month")

        # Validate period_months is in valid range
        if not (1 <= period_months <= 12):
            raise ValueError("period_months must be between 1 and 12")

        # Calculate end_date as first day of next month after period_months
        end_date = start_date + relativedelta(months=period_months)

        # Auto-generate name if not provided
        if not name:
            name = f"{start_date.strftime('%d.%m.%Y')} - {end_date.strftime('%d.%m.%Y')}"

        new_period = ServicePeriod(
            name=name,
            start_date=start_date,
            end_date=end_date,
            status="open",
            period_months=period_months,
        )
        self.session.add(new_period)
        await self.session.flush()  # Get period ID

        # Audit log
        await AuditService.log(
            session=self.session,
            entity_type="service_period",
            entity_id=new_period.id,
            action="create",
            actor_id=actor_id,
            changes={
                "name": name,
                "start_date": start_date.isoformat(),
                "end_date": end_date.isoformat(),
                "period_months": period_months,
                "status": "open",
            },
        )

        await self.session.commit()
        await self.session.refresh(new_period)

        logger.info(
            "Created new service period: id=%d, name=%s, dates=%s to %s, period_months=%d, actor_id=%s",
            new_period.id,
            name,
            start_date,
            end_date,
            period_months,
            actor_id,
        )

        return new_period

    async def update_electricity_data(
        self,
        period_id: int,
        electricity_start: Decimal,
        electricity_end: Decimal,
        electricity_multiplier: Decimal,
        electricity_rate: Decimal,
        electricity_losses: Decimal,
        actor_id: int | None = None,
    ) -> bool:
        """Update period with electricity readings only (no status change).

        Args:
            period_id: Period ID to update
            electricity_start: Starting meter reading
            electricity_end: Ending meter reading
            electricity_multiplier: Consumption multiplier
            electricity_rate: Rate per kWh
            electricity_losses: Transmission losses ratio
            actor_id: Admin user ID performing the update (optional)

        Returns:
            True if successful, False if period not found
        """
        period = await self.get_by_id(period_id)
        if not period:
            return False

        period.electricity_start = electricity_start
        period.electricity_end = electricity_end
        period.electricity_multiplier = electricity_multiplier
        period.electricity_rate = electricity_rate
        period.electricity_losses = electricity_losses

        # Audit log
        await AuditService.log(
            session=self.session,
            entity_type="service_period",
            entity_id=period.id,
            action="update",
            actor_id=actor_id,
            changes={
                "electricity_start": str(electricity_start),
                "electricity_end": str(electricity_end),
                "electricity_multiplier": str(electricity_multiplier),
                "electricity_rate": str(electricity_rate),
                "electricity_losses": str(electricity_losses),
            },
        )

        await self.session.commit()

        logger.info(
            "Updated period %d electricity data: start=%s, end=%s, multiplier=%s, rate=%s, losses=%s (actor_id=%s)",
            period_id,
            electricity_start,
            electricity_end,
            electricity_multiplier,
            electricity_rate,
            electricity_losses,
            actor_id,
        )

        return True

    async def update_budget_data(
        self,
        period_id: int,
        year_budget: Decimal,
        conservation_year_budget: Decimal,
        actor_id: int | None = None,
    ) -> bool:
        """Update period with budget data only (no status change).

        Args:
            period_id: Period ID to update
            year_budget: Annual MAIN budget
            conservation_year_budget: Annual CONSERVATION budget
            actor_id: Admin user ID performing the update (optional)

        Returns:
            True if successful, False if period not found
        """
        period = await self.get_by_id(period_id)
        if not period:
            return False

        period.year_budget = year_budget
        period.conservation_year_budget = conservation_year_budget

        # Audit log
        await AuditService.log(
            session=self.session,
            entity_type="service_period",
            entity_id=period.id,
            action="update",
            actor_id=actor_id,
            changes={
                "year_budget": str(year_budget),
                "conservation_year_budget": str(conservation_year_budget),
            },
        )

        await self.session.commit()

        logger.info(
            "Updated period %d budget data: year_budget=%s, conservation_year_budget=%s (actor_id=%s)",
            period_id,
            year_budget,
            conservation_year_budget,
            actor_id,
        )

        return True

    async def close_period(
        self,
        period_id: int,
        actor_id: int | None = None,
    ) -> bool:
        """Close a service period (status change only).

        Args:
            period_id: Period ID to close
            actor_id: Admin user ID who closed the period (optional)

        Returns:
            True if successful, False if period not found
        """
        period = await self.get_by_id(period_id)
        if not period:
            return False

        period.status = "closed"

        # Audit log
        await AuditService.log(
            session=self.session,
            entity_type="service_period",
            entity_id=period.id,
            action="close",
            actor_id=actor_id,
            changes={
                "status": "closed",
                "period_name": period.name,
            },
        )

        await self.session.commit()

        logger.info(
            "Closed period %d ('%s') (actor_id=%s)",
            period_id,
            period.name,
            actor_id,
        )
        return True


# Alias for backwards compatibility during migration
AsyncServicePeriodService = ServicePeriodService


__all__ = ["ServicePeriodService", "AsyncServicePeriodService", "PeriodDefaults", "PeriodInfo"]

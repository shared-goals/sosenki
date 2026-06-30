"""Service for managing electricity meter readings with audit logging."""

from datetime import date
from decimal import Decimal

from sqlalchemy import desc, select
from sqlalchemy.ext.asyncio import AsyncSession
from sqlalchemy.orm import selectinload

from src.models.electricity_reading import ElectricityReading
from src.models.property import Property
from src.services.audit_service import AuditService


class ElectricityReadingService:
    """Service for managing electricity meter readings."""

    def __init__(self, session: AsyncSession) -> None:
        """Initialize service with database session.

        Args:
            session: SQLAlchemy async session
        """
        self.session = session

    async def get_properties_with_latest_readings(
        self,
    ) -> list[tuple[Property, ElectricityReading | None]]:
        """Get all properties with their latest electricity readings.

        Returns:
            List of tuples (Property, ElectricityReading or None)
        """
        # Get all properties
        stmt = (
            select(Property)
            .options(selectinload(Property.owner))
            .where(Property.is_active)
            .order_by(Property.property_name)
        )
        result = await self.session.execute(stmt)
        properties = result.scalars().all()

        # Get latest reading for each property
        results = []
        for property_obj in properties:
            latest_reading = await self.get_latest_reading_for_property(property_obj.id)
            results.append((property_obj, latest_reading))

        return results

    async def get_reading_by_id(self, reading_id: int) -> ElectricityReading | None:
        """Get electricity reading by ID.

        Args:
            reading_id: Reading ID

        Returns:
            ElectricityReading object or None if not found
        """
        stmt = select(ElectricityReading).where(ElectricityReading.id == reading_id)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_reading_for_property(self, property_id: int) -> ElectricityReading | None:
        """Get latest electricity reading for a property.

        Args:
            property_id: Property ID

        Returns:
            Latest ElectricityReading object or None if no readings exist
        """
        stmt = (
            select(ElectricityReading)
            .where(ElectricityReading.property_id == property_id)
            .order_by(desc(ElectricityReading.reading_date))
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_reading_for_property_at_or_before(
        self,
        property_id: int,
        on_or_before: date,
    ) -> ElectricityReading | None:
        """Get latest electricity reading for a property at or before the given date."""
        stmt = (
            select(ElectricityReading)
            .where(
                ElectricityReading.property_id == property_id,
                ElectricityReading.reading_date <= on_or_before,
            )
            .order_by(desc(ElectricityReading.reading_date))
            .limit(1)
        )
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def get_latest_readings_for_properties_at_or_before(
        self,
        property_ids: list[int],
        on_or_before: date,
    ) -> dict[int, ElectricityReading | None]:
        """Get latest electricity readings for multiple properties at or before the given date.

        Returns a dict mapping property_id -> latest ElectricityReading (or None).
        """
        if not property_ids:
            return {}

        stmt = (
            select(ElectricityReading)
            .where(
                ElectricityReading.property_id.in_(property_ids),
                ElectricityReading.reading_date <= on_or_before,
            )
            .order_by(ElectricityReading.property_id.asc(), desc(ElectricityReading.reading_date))
        )
        result = await self.session.execute(stmt)
        rows = result.scalars().all()

        latest_by_property: dict[int, ElectricityReading | None] = dict.fromkeys(property_ids)
        for reading in rows:
            if reading.property_id is None:
                continue
            if latest_by_property.get(reading.property_id) is None:
                latest_by_property[reading.property_id] = reading

        return latest_by_property

    async def get_latest_reading_globally(self) -> ElectricityReading | None:
        """Get latest electricity reading across all properties.

        Returns:
            Latest ElectricityReading object or None if no readings exist
        """
        stmt = select(ElectricityReading).order_by(desc(ElectricityReading.reading_date)).limit(1)
        result = await self.session.execute(stmt)
        return result.scalar_one_or_none()

    async def create_reading(
        self,
        property_id: int,
        reading_date: date,
        reading_value: Decimal,
        actor_id: int,
    ) -> ElectricityReading:
        """Create new electricity reading.

        Args:
            property_id: Property ID
            reading_date: Reading date
            reading_value: Meter reading value
            actor_id: User ID performing the action (for audit logging)

        Returns:
            Created ElectricityReading object

        Raises:
            ValueError: If reading value is not positive or less than previous reading
        """
        # Validate positive value
        if reading_value <= 0:
            raise ValueError("Reading value must be positive")

        # Get previous reading to validate new value
        previous_reading = await self.get_latest_reading_for_property(property_id)
        if previous_reading and reading_value < previous_reading.reading_value:
            raise ValueError(
                f"Reading value ({reading_value}) must be greater than or equal to previous reading "
                f"({previous_reading.reading_value})"
            )

        # Create new reading
        reading = ElectricityReading(
            property_id=property_id,
            reading_date=reading_date,
            reading_value=reading_value,
        )
        self.session.add(reading)
        await self.session.flush()  # Ensure ID is assigned

        # Audit log
        await AuditService.log(
            session=self.session,
            entity_type="electricity_reading",
            entity_id=reading.id,
            action="create",
            actor_id=actor_id,
            changes={
                "property_id": property_id,
                "reading_date": reading_date.isoformat(),
                "reading_value": str(reading_value),
                "previous_value": str(previous_reading.reading_value) if previous_reading else None,
            },
        )

        return reading

    async def update_reading(
        self,
        reading_id: int,
        reading_date: date | None = None,
        reading_value: Decimal | None = None,
        actor_id: int | None = None,
    ) -> ElectricityReading:
        """Update electricity reading.

        Args:
            reading_id: Reading ID
            reading_date: New reading date (optional)
            reading_value: New meter reading value (optional)
            actor_id: User ID performing the action (for audit logging)

        Returns:
            Updated ElectricityReading object

        Raises:
            ValueError: If reading not found, value is not positive, or less than previous reading
        """
        reading = await self.get_reading_by_id(reading_id)
        if not reading:
            raise ValueError(f"Reading with ID {reading_id} not found")

        changes = {}
        old_values = {
            "reading_date": reading.reading_date.isoformat(),
            "reading_value": str(reading.reading_value),
        }

        # Update reading date if provided
        if reading_date is not None:
            reading.reading_date = reading_date
            changes["reading_date"] = {
                "old": old_values["reading_date"],
                "new": reading_date.isoformat(),
            }

        # Update reading value if provided
        if reading_value is not None:
            # Validate positive value
            if reading_value <= 0:
                raise ValueError("Reading value must be positive")

            # Get previous reading (before current one by date)
            # Use updated date if provided, otherwise use current date
            compare_date = reading_date if reading_date is not None else reading.reading_date
            stmt = (
                select(ElectricityReading)
                .where(
                    ElectricityReading.property_id == reading.property_id,
                    ElectricityReading.reading_date < compare_date,
                    ElectricityReading.id != reading.id,
                )
                .order_by(desc(ElectricityReading.reading_date))
                .limit(1)
            )
            result = await self.session.execute(stmt)
            previous_reading = result.scalar_one_or_none()

            # Validate new value is greater than or equal to previous
            if previous_reading and reading_value < previous_reading.reading_value:
                raise ValueError(
                    f"Reading value ({reading_value}) must be greater than or equal to previous reading "
                    f"({previous_reading.reading_value})"
                )

            reading.reading_value = reading_value
            changes["reading_value"] = {
                "old": old_values["reading_value"],
                "new": str(reading_value),
            }

        # Audit log if there were changes
        if changes and actor_id:
            await AuditService.log(
                session=self.session,
                entity_type="electricity_reading",
                entity_id=reading.id,
                action="update",
                actor_id=actor_id,
                changes=changes,
            )

        return reading

    async def delete_reading(self, reading_id: int, actor_id: int) -> None:
        """Delete electricity reading (hard delete).

        Args:
            reading_id: Reading ID to delete
            actor_id: User ID performing the action (for audit logging)

        Raises:
            ValueError: If reading not found
        """
        reading = await self.get_reading_by_id(reading_id)
        if not reading:
            raise ValueError(f"Reading with ID {reading_id} not found")

        # Capture values for audit log before deletion
        deleted_data = {
            "property_id": reading.property_id,
            "reading_date": reading.reading_date.isoformat(),
            "reading_value": str(reading.reading_value),
        }

        # Audit log before deletion
        await AuditService.log(
            session=self.session,
            entity_type="electricity_reading",
            entity_id=reading.id,
            action="delete",
            actor_id=actor_id,
            changes=deleted_data,
        )

        # Hard delete
        await self.session.delete(reading)

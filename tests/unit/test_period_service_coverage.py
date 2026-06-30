"""Unit tests for period_service.py."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models import Base
from src.models.account import Account
from src.models.service_period import ServicePeriod
from src.models.user import User
from src.services.period_service import PeriodDefaults, ServicePeriodService


@pytest.fixture
async def async_db_session():
    """Create async test database session."""
    engine = create_async_engine("sqlite+aiosqlite:///:memory:")
    async with engine.begin() as conn:
        await conn.run_sync(Base.metadata.create_all)

    async_session = async_sessionmaker(engine, class_=AsyncSession, expire_on_commit=False)
    async with async_session() as session:
        yield session


@pytest.fixture
async def admin_user(async_db_session):
    """Create admin user for tests."""
    user = User(telegram_id=123, is_active=True, is_administrator=True)
    async_db_session.add(user)
    await async_db_session.commit()
    return user


@pytest.fixture
async def account(async_db_session, admin_user):
    """Create account for tests."""
    acc = Account(user_id=admin_user.id, name="Test Account")
    async_db_session.add(acc)
    await async_db_session.commit()
    return acc


async def test_period_service_get_open_periods(async_db_session):
    """Test getting open periods."""
    # Create some test periods
    period1 = ServicePeriod(
        start_date=date(2025, 1, 1), end_date=date(2025, 1, 31), name="Period 1", status="open"
    )
    period2 = ServicePeriod(
        start_date=date(2025, 2, 1), end_date=date(2025, 2, 28), name="Period 2", status="open"
    )
    period3 = ServicePeriod(
        start_date=date(2024, 12, 1),
        end_date=date(2024, 12, 31),
        name="Period 3",
        status="closed",
    )
    async_db_session.add_all([period1, period2, period3])
    await async_db_session.commit()

    service = ServicePeriodService(async_db_session)
    open_periods = await service.get_open_periods()

    assert len(open_periods) == 2
    assert all(p.status == "open" for p in open_periods)


async def test_period_service_get_open_periods_limit(async_db_session):
    """Test getting all open periods without limit."""
    # Create 5 open periods
    for i in range(5):
        period = ServicePeriod(
            start_date=date(2025, i + 1, 1),
            end_date=date(2025, i + 1, 28),
            name=f"Period {i}",
            status="open",
        )
        async_db_session.add(period)
    await async_db_session.commit()

    service = ServicePeriodService(async_db_session)
    periods = await service.get_open_periods()

    assert len(periods) == 5


async def test_period_service_get_by_id(async_db_session):
    """Test getting period by id."""
    period = ServicePeriod(
        start_date=date(2025, 1, 1), end_date=date(2025, 1, 31), name="Test Period", status="open"
    )
    async_db_session.add(period)
    await async_db_session.commit()

    service = ServicePeriodService(async_db_session)
    retrieved = await service.get_by_id(period.id)

    assert retrieved is not None
    assert retrieved.id == period.id
    assert retrieved.name == "Test Period"


async def test_period_service_get_by_id_not_found(async_db_session):
    """Test getting non-existent period."""
    service = ServicePeriodService(async_db_session)
    result = await service.get_by_id(999)

    assert result is None


async def test_period_service_get_latest_period(async_db_session):
    """Test getting latest period."""
    period1 = ServicePeriod(
        start_date=date(2025, 1, 1), end_date=date(2025, 1, 31), name="Period 1", status="open"
    )
    period2 = ServicePeriod(
        start_date=date(2025, 2, 1), end_date=date(2025, 2, 28), name="Period 2", status="open"
    )
    async_db_session.add_all([period1, period2])
    await async_db_session.commit()

    service = ServicePeriodService(async_db_session)
    latest = await service.get_latest_period()

    assert latest is not None
    assert latest.name == "Period 2"


async def test_period_service_get_latest_period_none(async_db_session):
    """Test getting latest period when none exist."""
    service = ServicePeriodService(async_db_session)
    result = await service.get_latest_period()

    assert result is None


async def test_period_service_get_previous_period_defaults(async_db_session):
    """Test getting previous period defaults."""
    # Create previous period with electricity data
    # The method looks for end_date == current_start_date
    prev_period = ServicePeriod(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 2, 1),  # This should match the start date we query with
        name="Previous",
        status="closed",
        electricity_end=Decimal("200"),
        electricity_multiplier=Decimal("1.5"),
        electricity_rate=Decimal("5.50"),
        electricity_losses=Decimal("0.2"),
    )
    async_db_session.add(prev_period)
    await async_db_session.commit()

    service = ServicePeriodService(async_db_session)
    # Pass the same date as the previous period's end_date
    defaults = await service.get_previous_period_defaults(date(2025, 2, 1))

    assert defaults is not None
    # Decimal("200") converts to "200" but might have decimal places in string form
    assert "200" in defaults.electricity_end
    assert "1.5" in defaults.electricity_multiplier
    assert "5.5" in defaults.electricity_rate
    assert "0.2" in defaults.electricity_losses


async def test_period_service_get_previous_period_defaults_none(async_db_session):
    """Test getting defaults when no previous period exists."""
    service = ServicePeriodService(async_db_session)
    result = await service.get_previous_period_defaults(date(2025, 2, 1))

    # Should return empty PeriodDefaults, not None
    assert result is not None
    assert result.electricity_end is None
    assert result.electricity_multiplier is None
    assert result.electricity_rate is None
    assert result.electricity_losses is None


async def test_previous_period_prefers_populated_electricity_end(async_db_session):
    """Prefer period with populated electricity_end when end_date duplicates exist."""
    empty_defaults = ServicePeriod(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 2, 1),
        name="Previous Empty",
        status="closed",
        electricity_end=None,
        electricity_multiplier=Decimal("1.1"),
        electricity_rate=Decimal("5.1"),
        electricity_losses=Decimal("0.1"),
    )
    populated_defaults = ServicePeriod(
        start_date=date(2024, 12, 1),
        end_date=date(2025, 2, 1),
        name="Previous Populated",
        status="closed",
        electricity_end=Decimal("321"),
        electricity_multiplier=Decimal("1.2"),
        electricity_rate=Decimal("5.2"),
        electricity_losses=Decimal("0.2"),
    )
    async_db_session.add_all([empty_defaults, populated_defaults])
    await async_db_session.commit()

    service = ServicePeriodService(async_db_session)
    defaults = await service.get_previous_period_defaults(date(2025, 2, 1))

    assert defaults.electricity_end is not None
    assert "321" in defaults.electricity_end
    assert "1.2" in defaults.electricity_multiplier
    assert "5.2" in defaults.electricity_rate
    assert "0.2" in defaults.electricity_losses


async def test_previous_period_tie_breaks_by_recency(async_db_session):
    """When all candidates are populated, choose the most recent one deterministically."""
    older = ServicePeriod(
        start_date=date(2024, 11, 1),
        end_date=date(2025, 2, 1),
        name="Previous Older",
        status="closed",
        electricity_end=Decimal("111"),
        electricity_multiplier=Decimal("1.1"),
        electricity_rate=Decimal("5.1"),
        electricity_losses=Decimal("0.1"),
    )
    newer = ServicePeriod(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 2, 1),
        name="Previous Newer",
        status="closed",
        electricity_end=Decimal("222"),
        electricity_multiplier=Decimal("1.3"),
        electricity_rate=Decimal("5.3"),
        electricity_losses=Decimal("0.3"),
    )
    async_db_session.add_all([older, newer])
    await async_db_session.commit()

    service = ServicePeriodService(async_db_session)
    previous_period = await service.get_previous_period(date(2025, 2, 1))

    assert previous_period is not None
    assert previous_period.name == "Previous Newer"
    assert previous_period.electricity_end == Decimal("222")


async def test_period_service_list_periods(async_db_session):
    """Test listing periods."""
    period1 = ServicePeriod(
        start_date=date(2025, 1, 1), end_date=date(2025, 1, 31), name="P1", status="open"
    )
    period2 = ServicePeriod(
        start_date=date(2025, 2, 1), end_date=date(2025, 2, 28), name="P2", status="closed"
    )
    async_db_session.add_all([period1, period2])
    await async_db_session.commit()

    service = ServicePeriodService(async_db_session)
    periods = await service.list_periods()

    assert len(periods) == 2


async def test_period_service_list_periods_limit(async_db_session):
    """Test listing periods with limit."""
    for i in range(5):
        period = ServicePeriod(
            start_date=date(2025, i + 1, 1),
            end_date=date(2025, i + 1, 28),
            name=f"P{i}",
            status="open",
        )
        async_db_session.add(period)
    await async_db_session.commit()

    service = ServicePeriodService(async_db_session)
    periods = await service.list_periods(limit=2)

    assert len(periods) == 2


def test_period_defaults_dataclass():
    """Test PeriodDefaults dataclass."""
    defaults = PeriodDefaults(
        electricity_end="200",
        electricity_multiplier="1.5",
        electricity_rate="5.50",
        electricity_losses="0.2",
    )

    assert defaults.electricity_end == "200"
    assert defaults.electricity_multiplier == "1.5"
    assert defaults.electricity_rate == "5.50"
    assert defaults.electricity_losses == "0.2"

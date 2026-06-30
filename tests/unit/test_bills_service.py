"""Unit tests for bills_service.py."""

from datetime import date
from decimal import Decimal

import pytest
from sqlalchemy.ext.asyncio import AsyncSession, async_sessionmaker, create_async_engine

from src.models import Base
from src.models.account import Account
from src.models.bill import Bill, BillType
from src.models.electricity_reading import ElectricityReading
from src.models.property import Property
from src.models.service_period import ServicePeriod
from src.models.user import User
from src.services.bills_service import BillsService, OwnerShare


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
async def owner_users(async_db_session):
    """Create owner users for tests."""
    users = []
    for i in range(3):
        user = User(
            telegram_id=1000 + i,
            name=f"Owner {i}",  # Add required name field
            is_active=True,
            is_administrator=False,
        )
        async_db_session.add(user)
        users.append(user)
    await async_db_session.commit()
    return users


@pytest.fixture
async def owner_accounts(async_db_session, owner_users):
    """Create owner accounts for tests."""
    accounts = []
    for user in owner_users:
        account = Account(user_id=user.id, name=f"Owner Account {user.id}", account_type="owner")
        async_db_session.add(account)
        accounts.append(account)
    await async_db_session.commit()
    return accounts


@pytest.fixture
async def service_period(async_db_session):
    """Create a test service period."""
    period = ServicePeriod(
        start_date=date(2025, 1, 1),
        end_date=date(2025, 1, 31),
        name="January 2025",
        status="open",
    )
    async_db_session.add(period)
    await async_db_session.commit()
    return period


@pytest.fixture
async def properties_with_owners(async_db_session, owner_users):
    """Create properties for testing bill calculations."""
    # Non-conservation properties with share weights
    non_conservation = [
        Property(
            owner_id=owner_users[0].id,
            property_name="Apt 1",
            type="residential",
            is_active=True,
            is_conservation=False,
            share_weight=Decimal("30"),
        ),
        Property(
            owner_id=owner_users[0].id,
            property_name="Apt 2",
            type="residential",
            is_active=True,
            is_conservation=False,
            share_weight=Decimal("20"),
        ),
        Property(
            owner_id=owner_users[1].id,
            property_name="Apt 3",
            type="residential",
            is_active=True,
            is_conservation=False,
            share_weight=Decimal("50"),
        ),
    ]

    # Conservation properties with share weights
    conservation = [
        Property(
            owner_id=owner_users[2].id,
            property_name="Storage 1",
            type="commercial",
            is_active=True,
            is_conservation=True,
            share_weight=Decimal("40"),
        ),
        Property(
            owner_id=owner_users[0].id,
            property_name="Storage 2",
            type="commercial",
            is_active=True,
            is_conservation=True,
            share_weight=Decimal("60"),
        ),
    ]

    for prop in non_conservation + conservation:
        async_db_session.add(prop)

    await async_db_session.commit()
    return non_conservation + conservation


async def test_create_shared_electricity_bills(async_db_session, owner_accounts, service_period):
    """Test creating shared electricity bills."""
    bills_service = BillsService(async_db_session)

    owner_shares = [
        OwnerShare(
            user_id=owner_accounts[0].user_id,
            user_name="User 1",
            total_share_weight=10,
            calculated_bill_amount=Decimal("100.00"),
        ),
        OwnerShare(
            user_id=owner_accounts[1].user_id,
            user_name="User 2",
            total_share_weight=15,
            calculated_bill_amount=Decimal("150.00"),
        ),
        OwnerShare(
            user_id=owner_accounts[2].user_id,
            user_name="User 3",
            total_share_weight=20,
            calculated_bill_amount=Decimal("200.00"),
        ),
    ]

    bills_created = await bills_service.create_shared_electricity_bills(
        period_id=service_period.id,
        owner_shares=owner_shares,
        actor_id=None,
    )

    assert bills_created == 3

    # Verify bills were created in database
    bills = await async_db_session.execute(
        __import__("sqlalchemy").select(Bill).filter(Bill.service_period_id == service_period.id)
    )
    created_bills = list(bills.scalars().all())
    assert len(created_bills) == 3

    for bill in created_bills:
        assert bill.bill_type == BillType.SHARED_ELECTRICITY
        assert bill.property_id is None


async def test_create_shared_electricity_bills_with_missing_account(
    async_db_session, service_period
):
    """Test creating shared electricity bills when account doesn't exist."""
    bills_service = BillsService(async_db_session)

    # User that doesn't have an account
    owner_shares = [
        OwnerShare(
            user_id=9999,
            user_name="Unknown User",
            total_share_weight=10,
            calculated_bill_amount=Decimal("100.00"),
        ),
    ]

    bills_created = await bills_service.create_shared_electricity_bills(
        period_id=service_period.id,
        owner_shares=owner_shares,
        actor_id=None,
    )

    # Should skip missing accounts
    assert bills_created == 0


async def test_calculate_personal_electricity_bills_skips_missing_readings(
    async_db_session,
    owner_users,
    service_period,
):
    bills_service = BillsService(async_db_session)

    prop_with_readings = Property(
        owner_id=owner_users[0].id,
        property_name="Apt With Readings",
        type="residential",
        is_active=True,
        is_conservation=False,
        share_weight=Decimal("10"),
    )
    prop_without_readings = Property(
        owner_id=owner_users[1].id,
        property_name="Apt Without Readings",
        type="residential",
        is_active=True,
        is_conservation=False,
        share_weight=Decimal("10"),
    )
    async_db_session.add_all([prop_with_readings, prop_without_readings])
    await async_db_session.commit()

    async_db_session.add_all(
        [
            ElectricityReading(
                property_id=prop_with_readings.id,
                reading_date=date(2024, 12, 31),
                reading_value=Decimal("100"),
            ),
            ElectricityReading(
                property_id=prop_with_readings.id,
                reading_date=date(2025, 1, 31),
                reading_value=Decimal("160"),
            ),
        ]
    )
    await async_db_session.commit()

    personal_bills, total = await bills_service.calculate_personal_electricity_bills_from_readings(
        service_period=service_period,
        electricity_rate=Decimal("2"),
    )

    assert len(personal_bills) == 1
    assert personal_bills[0].property_id == prop_with_readings.id
    assert personal_bills[0].consumption_kwh == Decimal("60")
    assert personal_bills[0].bill_amount == Decimal("120.00")
    assert total == Decimal("120.00")


async def test_calculate_main_bills(async_db_session, properties_with_owners):
    """Test calculating MAIN bills for all owners (based on all properties)."""
    bills_service = BillsService(async_db_session)

    year_budget = Decimal("12000")  # Annual budget
    period_months = 1  # One month

    calculations = await bills_service.calculate_main_bills(year_budget, period_months)

    # Should calculate for ALL owners (all active properties contribute to main bills)
    assert len(calculations) == 3  # All three owners have active properties

    # Verify results are tuples of (user_id, amount)
    for user_id, amount in calculations:
        assert isinstance(user_id, int)
        assert isinstance(amount, Decimal)
        assert amount > 0


async def test_calculate_main_bills_with_invalid_inputs(async_db_session, properties_with_owners):
    """Test calculate_main_bills with invalid parameters."""
    bills_service = BillsService(async_db_session)

    # Negative budget
    result = await bills_service.calculate_main_bills(Decimal("-100"), 1)
    assert result == []

    # Invalid month count
    result = await bills_service.calculate_main_bills(Decimal("100"), 0)
    assert result == []

    result = await bills_service.calculate_main_bills(Decimal("100"), 13)
    assert result == []


async def test_calculate_main_bills_no_properties(async_db_session):
    """Test calculate_main_bills with no properties in database."""
    bills_service = BillsService(async_db_session)

    result = await bills_service.calculate_main_bills(Decimal("12000"), 1)
    assert result == []


async def test_calculate_conservation_bills(async_db_session, properties_with_owners):
    """Test calculating CONSERVATION bills for conservation properties."""
    bills_service = BillsService(async_db_session)

    conservation_budget = Decimal("3000")  # Annual conservation budget
    period_months = 3  # Three months

    calculations = await bills_service.calculate_conservation_bills(
        conservation_budget, period_months
    )

    # Should calculate for owners with conservation properties
    assert len(calculations) >= 1

    # Verify results are valid
    for user_id, amount in calculations:
        assert isinstance(user_id, int)
        assert isinstance(amount, Decimal)
        assert amount > 0


async def test_calculate_conservation_bills_normalization(async_db_session, owner_users):
    """Test that conservation bills normalize to 100% total weight."""
    # Create two properties with different weights
    prop1 = Property(
        owner_id=owner_users[0].id,
        property_name="Prop1",
        type="residential",
        is_active=True,
        is_conservation=True,
        share_weight=Decimal("25"),
    )
    prop2 = Property(
        owner_id=owner_users[1].id,
        property_name="Prop2",
        type="residential",
        is_active=True,
        is_conservation=True,
        share_weight=Decimal("75"),
    )
    async_db_session.add_all([prop1, prop2])
    await async_db_session.commit()

    bills_service = BillsService(async_db_session)

    conservation_budget = Decimal("1200")  # $100/month
    period_months = 1

    calculations = await bills_service.calculate_conservation_bills(
        conservation_budget, period_months
    )

    # Should distribute budget across both owners
    assert len(calculations) == 2
    # Both should have positive amounts
    for _user_id, amount in calculations:
        assert amount > 0


async def test_calculate_conservation_bills_with_invalid_inputs(
    async_db_session, properties_with_owners
):
    """Test calculate_conservation_bills with invalid parameters."""
    bills_service = BillsService(async_db_session)

    # Negative budget
    result = await bills_service.calculate_conservation_bills(Decimal("-100"), 1)
    assert result == []

    # Invalid month count
    result = await bills_service.calculate_conservation_bills(Decimal("100"), 0)
    assert result == []


async def test_create_main_bills(async_db_session, owner_accounts, service_period):
    """Test creating MAIN bills from calculations."""
    bills_service = BillsService(async_db_session)

    calculations = [
        (owner_accounts[0].user_id, Decimal("500.00")),
        (owner_accounts[1].user_id, Decimal("750.00")),
        (owner_accounts[2].user_id, Decimal("1000.00")),
    ]

    bills_created = await bills_service.create_main_bills(
        period_id=service_period.id,
        calculations=calculations,
        actor_id=None,
    )

    assert bills_created == 3

    # Verify bills have correct type
    from sqlalchemy import select

    bills = await async_db_session.execute(
        select(Bill).filter(Bill.service_period_id == service_period.id)
    )
    created_bills = list(bills.scalars().all())
    assert all(bill.bill_type == BillType.MAIN for bill in created_bills)


async def test_create_main_bills_prevents_duplicates(async_db_session, owner_accounts, service_period):
    """Creating MAIN bills twice for the same period should fail."""
    bills_service = BillsService(async_db_session)

    calculations = [
        (owner_accounts[0].user_id, Decimal("500.00")),
    ]

    created = await bills_service.create_main_bills(
        period_id=service_period.id,
        calculations=calculations,
        actor_id=None,
    )
    assert created == 1

    with pytest.raises(ValueError, match="MAIN bills already exist"):
        await bills_service.create_main_bills(
            period_id=service_period.id,
            calculations=calculations,
            actor_id=None,
        )


async def test_create_main_bills_with_missing_account(async_db_session, service_period):
    """Test create_main_bills skips missing accounts."""
    bills_service = BillsService(async_db_session)

    calculations = [
        (9999, Decimal("500.00")),  # Non-existent user
    ]

    bills_created = await bills_service.create_main_bills(
        period_id=service_period.id,
        calculations=calculations,
        actor_id=None,
    )

    assert bills_created == 0


async def test_create_conservation_bills(async_db_session, owner_accounts, service_period):
    """Test creating CONSERVATION bills from calculations."""
    bills_service = BillsService(async_db_session)

    calculations = [
        (owner_accounts[0].user_id, Decimal("250.00")),
        (owner_accounts[2].user_id, Decimal("300.00")),
    ]

    bills_created = await bills_service.create_conservation_bills(
        period_id=service_period.id,
        calculations=calculations,
        actor_id=None,
    )

    assert bills_created == 2

    # Verify bills have correct type
    from sqlalchemy import select

    bills = await async_db_session.execute(
        select(Bill).filter(Bill.service_period_id == service_period.id)
    )
    created_bills = list(bills.scalars().all())
    assert all(bill.bill_type == BillType.CONSERVATION for bill in created_bills)


async def test_create_conservation_bills_prevents_duplicates(
    async_db_session, owner_accounts, service_period
):
    """Creating CONSERVATION bills twice for the same period should fail."""
    bills_service = BillsService(async_db_session)

    calculations = [
        (owner_accounts[0].user_id, Decimal("250.00")),
    ]

    created = await bills_service.create_conservation_bills(
        period_id=service_period.id,
        calculations=calculations,
        actor_id=None,
    )
    assert created == 1

    with pytest.raises(ValueError, match="CONSERVATION bills already exist"):
        await bills_service.create_conservation_bills(
            period_id=service_period.id,
            calculations=calculations,
            actor_id=None,
        )


async def test_count_budget_bills_for_period(async_db_session, owner_accounts, service_period):
    """Budget bill counter should include MAIN and CONSERVATION types."""
    bills_service = BillsService(async_db_session)

    await bills_service.create_main_bills(
        period_id=service_period.id,
        calculations=[(owner_accounts[0].user_id, Decimal("100.00"))],
        actor_id=None,
    )

    # Create conservation for a different period to keep per-period count deterministic.
    other_period = ServicePeriod(
        start_date=date(2025, 2, 1),
        end_date=date(2025, 2, 28),
        name="February 2025",
        status="open",
    )
    async_db_session.add(other_period)
    await async_db_session.commit()

    await bills_service.create_conservation_bills(
        period_id=other_period.id,
        calculations=[(owner_accounts[0].user_id, Decimal("100.00"))],
        actor_id=None,
    )

    count_current = await bills_service.count_budget_bills_for_period(service_period.id)
    count_other = await bills_service.count_budget_bills_for_period(other_period.id)

    assert count_current == 1
    assert count_other == 1


async def test_bills_service_with_multiple_properties_per_owner(async_db_session, owner_users):
    """Test bill calculations when owner has multiple properties."""
    # Create multiple properties for one owner
    for i in range(3):
        prop = Property(
            owner_id=owner_users[0].id,
            property_name=f"Apt {i}",
            type="residential",
            is_active=True,
            is_conservation=False,
            share_weight=Decimal("20"),
        )
        async_db_session.add(prop)
    await async_db_session.commit()

    bills_service = BillsService(async_db_session)

    calculations = await bills_service.calculate_main_bills(Decimal("1200"), 1)

    # Should have one entry for the owner with aggregated sum of all properties
    owner_totals = dict(calculations)
    assert owner_users[0].id in owner_totals
    # Amount should be positive (3 properties * 20% share = 60%)
    assert owner_totals[owner_users[0].id] > 0


__all__ = [
    "test_create_shared_electricity_bills",
    "test_calculate_main_bills",
    "test_calculate_conservation_bills",
]

"""SQLAlchemy base model with common fields and model exports."""

from datetime import datetime, timezone

from sqlalchemy import DateTime
from sqlalchemy.orm import Mapped, declarative_base, mapped_column

# Base class for all models
Base = declarative_base()


class BaseModel:
    """Base model with common timestamp fields."""

    id: Mapped[int] = mapped_column(primary_key=True, autoincrement=True)
    created_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        nullable=False,
    )
    updated_at: Mapped[datetime] = mapped_column(
        DateTime(timezone=True),
        default=lambda: datetime.now(timezone.utc),
        onupdate=lambda: datetime.now(timezone.utc),
        nullable=False,
    )


# Import models to register them with Base (after Base is defined)
# This must be after Base declaration to avoid circular imports
from src.models.access_request import AccessRequest, RequestStatus  # noqa: E402
from src.models.account import Account, AccountType  # noqa: E402
from src.models.auction_bid import AuctionBid  # noqa: E402
from src.models.audit_log import AuditLog  # noqa: E402
from src.models.bill import Bill, BillType  # noqa: E402
from src.models.budget_item import AllocationStrategy, BudgetItem  # noqa: E402
from src.models.electricity_reading import ElectricityReading  # noqa: E402
from src.models.property import Property  # noqa: E402
from src.models.service_period import PeriodStatus, ServicePeriod  # noqa: E402
from src.models.transaction import Transaction  # noqa: E402
from src.models.user import User  # noqa: E402

__all__ = [
    "Base",
    "BaseModel",
    "User",
    "Account",
    "AccountType",
    "Transaction",
    "AccessRequest",
    "RequestStatus",
    "Property",
    "ServicePeriod",
    "PeriodStatus",
    "BudgetItem",
    "AllocationStrategy",
    "ElectricityReading",
    "Bill",
    "BillType",
    "AuditLog",
    "AuctionBid",
]

"""Bill ORM model for tracking all billing types (electricity, conservation, etc)."""

from decimal import Decimal
from enum import Enum

from sqlalchemy import ForeignKey, Index, Numeric, String, text
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models import Base, BaseModel


class BillType(str, Enum):
    """Types of bills that can be tracked."""

    ELECTRICITY = "electricity"
    """Individual property electricity bill"""

    SHARED_ELECTRICITY = "shared_electricity"
    """Common area (communal) electricity bill"""

    CONSERVATION = "conservation"
    """Conservation/maintenance bill"""

    MAIN = "main"
    """Main/general bill"""


class Bill(Base, BaseModel):
    """
    Unified model representing all types of bills for users or properties.

    Polymorphic design allows billing for both property-level and user-level tracking.
    A bill_type field distinguishes between electricity, shared electricity, conservation, and main bills.
    """

    __tablename__ = "bills"

    # Foreign keys
    service_period_id: Mapped[int] = mapped_column(
        ForeignKey("service_periods.id"),
        nullable=False,
        index=True,
        comment="Associated service/billing period",
    )

    account_id: Mapped[int | None] = mapped_column(
        ForeignKey("accounts.id"),
        nullable=True,
        index=True,
        comment="Account this bill belongs to (user or organization account)",
    )

    property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id"),
        nullable=True,
        index=True,
        comment="Property this bill belongs to",
    )

    # Bill type
    bill_type: Mapped[BillType] = mapped_column(
        nullable=False,
        index=True,
        comment="Type of bill: electricity, shared_electricity, conservation, or main",
    )

    # Bill details
    bill_amount: Mapped[Decimal] = mapped_column(
        Numeric(10, 2),
        nullable=False,
        comment="Bill amount in rubles",
    )

    comment: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="Optional comment (e.g., property name when property_id not found)",
    )

    # Relationships
    service_period: Mapped["ServicePeriod"] = relationship(  # noqa: F821
        "ServicePeriod",
        foreign_keys=[service_period_id],
    )

    account: Mapped["Account | None"] = relationship(  # noqa: F821
        "Account",
        foreign_keys=[account_id],
        back_populates="bills",
    )

    property: Mapped["Property | None"] = relationship(  # noqa: F821
        "Property",
        foreign_keys=[property_id],
    )

    # Indexes for common queries
    __table_args__ = (
        Index("idx_bill_period_account", "service_period_id", "account_id"),
        Index("idx_bill_period_property", "service_period_id", "property_id"),
        Index("idx_bill_type", "bill_type"),
        Index("idx_bill_period_type", "service_period_id", "bill_type"),
        Index(
            "uq_bill_main_period_account",
            "service_period_id",
            "account_id",
            unique=True,
            sqlite_where=text("bill_type = 'main'"),
            postgresql_where=text("bill_type = 'main'"),
        ),
        Index(
            "uq_bill_conservation_period_account",
            "service_period_id",
            "account_id",
            unique=True,
            sqlite_where=text("bill_type = 'conservation'"),
            postgresql_where=text("bill_type = 'conservation'"),
        ),
    )

    def __repr__(self) -> str:
        return (
            f"<Bill(id={self.id}, service_period_id={self.service_period_id}, "
            f"bill_type={self.bill_type}, account_id={self.account_id}, "
            f"property_id={self.property_id}, bill_amount={self.bill_amount})>"
        )


__all__ = ["Bill", "BillType"]

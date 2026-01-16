"""Property ORM model for managing physical properties/houses with ownership and share weights."""

from decimal import Decimal

from sqlalchemy import Boolean, ForeignKey, Index, Numeric, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models import Base, BaseModel


class Property(Base, BaseModel):
    """Model representing a physical property/house with owner assignment and allocation weight.

    The share_weight field is critical for proportional expense allocations and makes the system
    DRY by storing this coefficient once (instead of hardcoding allocation percentages).

    Active properties are used in FIXED_FEE allocations to determine which owners receive equal
    charges during a period. The is_ready flag tracks whether a property is prepared for tenants
    or habitation.
    """

    __tablename__ = "properties"

    # Ownership and identification
    owner_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    property_name: Mapped[str] = mapped_column(
        String(100),
        nullable=False,
    )
    type: Mapped[str] = mapped_column(
        String(50),
        nullable=False,
    )

    # Allocation weight for proportional distribution (e.g., 2.5, 1.0)
    # Using Numeric(10, 4) to preserve precision for accurate bill calculations
    share_weight: Mapped[Decimal | None] = mapped_column(
        Numeric(10, 6),
        nullable=True,
    )

    # Active status for determining property participation in period
    is_active: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
    )

    # Ready status for property occupancy/tenant readiness
    is_ready: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
        comment="Whether property is ready for tenants or habitation",
    )

    # Tenant occupancy status
    is_for_tenant: Mapped[bool] = mapped_column(
        Boolean,
        default=False,
        nullable=False,
        comment="Whether property is for tenant",
    )

    # Conservation participation flag for conservation bills
    is_conservation: Mapped[bool] = mapped_column(
        Boolean,
        default=True,
        nullable=False,
        index=True,
        comment="Whether property participates in conservation bills calculation",
    )

    # Property photo gallery URL
    photo_link: Mapped[str | None] = mapped_column(
        String(500),
        nullable=True,
        comment="URL to property's photo gallery",
    )

    # Selling price (integer value)
    sale_price: Mapped[int | None] = mapped_column(
        nullable=True,
        comment="Selling price of the property (integer price value)",
    )

    # Self-referential FK for hierarchical property relationships
    main_property_id: Mapped[int | None] = mapped_column(
        ForeignKey("properties.id"),
        nullable=True,
        index=True,
        comment="ID of main property if this is an additional property",
    )

    # Relationships
    owner: Mapped["User"] = relationship(  # noqa: F821
        "User",
        back_populates="properties",
        foreign_keys=[owner_id],
    )

    # Self-referential relationship for additional properties
    main_property: Mapped["Property | None"] = relationship(
        "Property",
        remote_side="Property.id",
        back_populates="additional_properties",
        foreign_keys="Property.main_property_id",
    )

    additional_properties: Mapped[list["Property"]] = relationship(
        "Property",
        back_populates="main_property",
        foreign_keys="Property.main_property_id",
        viewonly=True,
    )

    # Indexes for common queries
    __table_args__ = (
        Index("idx_owner_active", "owner_id", "is_active"),
        Index("idx_owner_ready", "owner_id", "is_ready"),
    )

    def __repr__(self) -> str:
        return (
            f"<Property(id={self.id}, owner_id={self.owner_id}, "
            f"main_property_id={self.main_property_id}, "
            f"property_name={self.property_name!r}, type={self.type!r}, "
            f"share_weight={self.share_weight}, is_active={self.is_active}, "
            f"is_ready={self.is_ready}, is_for_tenant={self.is_for_tenant}, "
            f"photo_link={self.photo_link!r}, sale_price={self.sale_price})>"
        )


__all__ = ["Property"]

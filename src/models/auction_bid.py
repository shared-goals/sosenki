"""Auction bid ORM model for Invest/Auction feature."""

from datetime import datetime
from typing import TYPE_CHECKING

from sqlalchemy import DateTime, ForeignKey, Index, Integer, String
from sqlalchemy.orm import Mapped, mapped_column, relationship

from src.models import Base, BaseModel

if TYPE_CHECKING:  # pragma: no cover
    from src.models.property import Property
    from src.models.user import User


class AuctionBid(Base, BaseModel):
    """Bid placed by an investor for a property.

    Notes:
    - Multiple bids per investor per property are allowed.
    - Only bids with status='active' participate in max-bid calculations.
    - Canceled bids remain in the database and are shown in the journal.
    """

    __tablename__ = "auction_bids"

    property_id: Mapped[int] = mapped_column(
        ForeignKey("properties.id"),
        nullable=False,
        index=True,
    )

    investor_id: Mapped[int] = mapped_column(
        ForeignKey("users.id"),
        nullable=False,
        index=True,
    )

    amount: Mapped[int] = mapped_column(
        Integer,
        nullable=False,
        comment="Bid amount in integer currency units",
    )

    status: Mapped[str] = mapped_column(
        String(20),
        nullable=False,
        default="active",
        comment="Bid status: active|canceled",
    )

    canceled_at: Mapped[datetime | None] = mapped_column(
        DateTime(timezone=True),
        nullable=True,
        comment="Timestamp when the bid was canceled (journal event)",
    )

    property: Mapped["Property"] = relationship(
        "Property",
        foreign_keys=[property_id],
    )

    investor: Mapped["User"] = relationship(
        "User",
        foreign_keys=[investor_id],
    )

    __table_args__ = (
        Index("idx_auction_bid_property_status", "property_id", "status"),
        Index("idx_auction_bid_investor_status", "investor_id", "status"),
    )


__all__ = ["AuctionBid"]

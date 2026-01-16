"""Auction service for Invest/Auction feature.

Implements:
- Listing auction properties (sale_price != NULL)
- Placing bids with min increment rule
- Canceling bids (kept for journal)
- Building a journal view (bid/cancel events)

Business rules:
- Only active bids participate in max-bid calculations.
- Bid amount must be integer > 0.
- New bid must satisfy: amount >= ceil(max_active * 1.1).
- Multiple bids per investor per property are allowed.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from datetime import datetime, timezone
from decimal import ROUND_CEILING, Decimal

from fastapi import HTTPException
from sqlalchemy import func, select
from sqlalchemy.ext.asyncio import AsyncSession

from src.models.auction_bid import AuctionBid
from src.models.property import Property
from src.models.user import User


def _min_required_bid(max_active: int) -> int:
    # Allow tuning via env without redeploying code.
    # - AUCTION_MIN_BID: int (default 1)
    # - AUCTION_BID_INCREMENT: decimal multiplier (default 1.1)
    try:
        min_bid = int(os.getenv("AUCTION_MIN_BID", "1"))
    except Exception:
        min_bid = 1

    try:
        increment = Decimal(os.getenv("AUCTION_BID_INCREMENT", "1.1"))
    except Exception:
        increment = Decimal("1.1")

    if min_bid < 1:
        min_bid = 1
    if increment <= 0:
        increment = Decimal("1.1")

    if max_active <= 0:
        return min_bid

    required = (Decimal(max_active) * increment).to_integral_value(rounding=ROUND_CEILING)
    return max(min_bid, int(required))


@dataclass(frozen=True)
class AuctionTotals:
    target_sum: int
    collectable_sum: int


@dataclass(frozen=True)
class AuctionBidInfo:
    bid_id: int
    amount: int
    created_at: datetime


@dataclass(frozen=True)
class AuctionPropertyInfo:
    property_id: int
    property_name: str
    property_type: str
    share_weight: str | None
    photo_link: str | None
    sale_price: int | None
    main_property_id: int | None
    is_ready: bool
    is_for_tenant: bool
    max_active_bid: int
    min_next_bid: int
    my_active_bids: list[AuctionBidInfo]


@dataclass(frozen=True)
class AuctionJournalEntry:
    timestamp: datetime
    action: str  # "bid" | "cancel"
    property_id: int
    property_name: str
    amount: int
    status: str  # "active" | "canceled"
    bidder_name: str | None = None


class AuctionService:
    def __init__(self, session: AsyncSession):
        self.session = session

    async def _require_investor(self, user: User) -> None:
        if not user.is_investor:
            raise HTTPException(status_code=403, detail="NOT_INVESTOR")

    async def list_auction(
        self, viewer: User
    ) -> tuple[AuctionTotals, list[AuctionPropertyInfo], list[AuctionJournalEntry]]:
        """List auction data for a user.

        Args:
            viewer: The user viewing the auction (may be target user if admin impersonating)
        """
        await self._require_investor(viewer)

        properties_stmt = (
            select(Property)
            .where(Property.is_active == True, Property.sale_price.is_not(None))  # noqa: E712
            .order_by(Property.id)
        )
        properties_result = await self.session.execute(properties_stmt)
        properties = properties_result.scalars().all()

        target_sum = sum((p.sale_price or 0) for p in properties)

        # Max active bid per property
        max_stmt = (
            select(AuctionBid.property_id, func.max(AuctionBid.amount))
            .where(AuctionBid.status == "active")
            .group_by(AuctionBid.property_id)
        )
        max_rows = (await self.session.execute(max_stmt)).all()
        max_by_property: dict[int, int] = {
            pid: int(max_amount or 0) for pid, max_amount in max_rows
        }

        collectable_sum = sum(max_by_property.get(p.id, 0) for p in properties)

        # My active bids (relative to viewer)
        my_bids_stmt = (
            select(AuctionBid)
            .where(AuctionBid.investor_id == viewer.id, AuctionBid.status == "active")
            .order_by(AuctionBid.created_at.desc())
        )
        my_bids = (await self.session.execute(my_bids_stmt)).scalars().all()
        my_bids_by_property: dict[int, list[AuctionBidInfo]] = {}
        for bid in my_bids:
            my_bids_by_property.setdefault(bid.property_id, []).append(
                AuctionBidInfo(bid_id=bid.id, amount=bid.amount, created_at=bid.created_at)
            )

        property_infos: list[AuctionPropertyInfo] = []
        for prop in properties:
            max_active = max_by_property.get(prop.id, 0)
            min_next = _min_required_bid(max_active)
            property_infos.append(
                AuctionPropertyInfo(
                    property_id=prop.id,
                    property_name=prop.property_name,
                    property_type=prop.type,
                    share_weight=str(prop.share_weight) if prop.share_weight is not None else None,
                    photo_link=prop.photo_link,
                    sale_price=prop.sale_price,
                    main_property_id=prop.main_property_id,
                    is_ready=prop.is_ready,
                    is_for_tenant=prop.is_for_tenant,
                    max_active_bid=max_active,
                    min_next_bid=min_next,
                    my_active_bids=my_bids_by_property.get(prop.id, []),
                )
            )

        journal = await self.get_journal(viewer)
        totals = AuctionTotals(target_sum=target_sum, collectable_sum=collectable_sum)
        return totals, property_infos, journal

    async def place_bid(
        self, authenticated_user: User, *, property_id: int, amount: int
    ) -> AuctionBid:
        await self._require_investor(authenticated_user)

        if amount <= 0:
            raise HTTPException(status_code=400, detail="INVALID_BID_AMOUNT")

        prop = await self.session.get(Property, property_id)
        if not prop or not prop.is_active or prop.sale_price is None:
            raise HTTPException(status_code=404, detail="PROPERTY_NOT_IN_AUCTION")

        max_active_stmt = select(func.max(AuctionBid.amount)).where(
            AuctionBid.property_id == property_id, AuctionBid.status == "active"
        )
        max_active = int((await self.session.execute(max_active_stmt)).scalar() or 0)

        min_required = _min_required_bid(max_active)
        if amount < min_required:
            raise HTTPException(status_code=400, detail="BID_TOO_LOW")

        bid = AuctionBid(
            property_id=property_id,
            investor_id=authenticated_user.id,
            amount=int(amount),
            status="active",
        )
        self.session.add(bid)
        await self.session.flush()
        return bid

    async def cancel_bid(self, investor: User, *, bid_id: int) -> AuctionBid:
        """Cancel an active bid.

        Args:
            investor: The investor user canceling the bid (may be target user if admin impersonating)
            bid_id: ID of the bid to cancel
        """
        await self._require_investor(investor)

        bid = await self.session.get(AuctionBid, bid_id)
        if not bid:
            raise HTTPException(status_code=404, detail="BID_NOT_FOUND")

        if bid.investor_id != investor.id:
            raise HTTPException(status_code=403, detail="NOT_BID_OWNER")

        if bid.status != "active":
            raise HTTPException(status_code=400, detail="BID_NOT_ACTIVE")

        bid.status = "canceled"
        bid.canceled_at = datetime.now(timezone.utc)
        await self.session.flush()
        return bid

    async def get_journal(self, viewer: User, *, limit: int = 200) -> list[AuctionJournalEntry]:
        """Get auction journal entries.

        Args:
            viewer: The user viewing the journal (may be target user if admin impersonating)
            limit: Maximum number of entries to return

        Note:
            Bidder names are hidden when viewing as non-admin (including admin impersonating investor).
        """
        await self._require_investor(viewer)

        # Fetch bids + joins needed for journal.
        # We build two journal events per bid: creation and (optional) cancellation.
        bids_stmt = (
            select(AuctionBid, Property, User)
            .join(Property, Property.id == AuctionBid.property_id)
            .join(User, User.id == AuctionBid.investor_id)
            .where(Property.sale_price.is_not(None))
            .order_by(AuctionBid.created_at.desc())
            .limit(limit)
        )
        rows = (await self.session.execute(bids_stmt)).all()

        entries: list[AuctionJournalEntry] = []
        for bid, prop, bidder in rows:
            # Show bidder names ONLY if viewer is admin AND not viewing as impersonated user
            # Since we receive target_user when impersonating, viewer.is_administrator will be False
            show_names = viewer.is_administrator

            entries.append(
                AuctionJournalEntry(
                    timestamp=bid.created_at,
                    action="bid",
                    property_id=prop.id,
                    property_name=prop.property_name,
                    amount=bid.amount,
                    status="active" if bid.status == "active" else "canceled",
                    bidder_name=bidder.name if show_names else None,
                )
            )
            if bid.canceled_at:
                entries.append(
                    AuctionJournalEntry(
                        timestamp=bid.canceled_at,
                        action="cancel",
                        property_id=prop.id,
                        property_name=prop.property_name,
                        amount=bid.amount,
                        status="canceled",
                        bidder_name=bidder.name if show_names else None,
                    )
                )

        entries.sort(key=lambda e: e.timestamp, reverse=True)
        return entries[:limit]


__all__ = ["AuctionService", "AuctionTotals", "AuctionPropertyInfo", "AuctionJournalEntry"]

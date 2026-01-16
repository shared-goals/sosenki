"""Mini App API endpoints."""

import logging
import os
import time
from typing import Any

from fastapi import APIRouter, Body, Depends, Header, HTTPException
from pydantic import BaseModel, ConfigDict
from sqlalchemy import select
from sqlalchemy.ext.asyncio import AsyncSession

from src.api.bot_context import get_bot_app  # type: ignore[misc]
from src.models.account import Account, AccountType
from src.models.auction_bid import AuctionBid
from src.models.property import Property
from src.models.transaction import Transaction
from src.models.user import User
from src.services import get_async_session
from src.services.auction_service import AuctionService
from src.services.audit_service import AuditService
from src.services.auth_service import (
    _extract_init_data,  # type: ignore[reportPrivateUsage]
    authorize_account_access,
    authorize_account_access_for_roles,
    authorize_user_context_access,
    get_authenticated_user,
    verify_telegram_auth,
)
from src.services.locale_service import format_currency
from src.services.localizer import t
from src.services.notification_service import NotificationService
from src.services.transaction_service import TransactionService
from src.services.user_service import UserService, UserStatusService

logger = logging.getLogger(__name__)


def _log_debug(
    endpoint: str, start_time: float, telegram_id: int, user: Any, **kwargs: Any
) -> None:
    """Log API request with timing and user context at INFO level.

    Args:
        endpoint: Endpoint name (e.g., 'init', 'bills')
        start_time: Request start time from time.time()
        telegram_id: Telegram user ID
        user: Authenticated user object
        **kwargs: Additional fields to log (account_id, count, scope, etc.)
    """
    duration_ms = int((time.time() - start_time) * 1000)
    user_id = getattr(user, "id", "?")
    extra = " ".join(f"{k}={v}" for k, v in kwargs.items())
    logger.info(
        "mini_app.%s: telegram_id=%d user_id=%s %sduration_ms=%d",
        endpoint,
        telegram_id,
        user_id,
        f"{extra} " if extra else "",
        duration_ms,
    )


# Create router for Mini App endpoints
router = APIRouter(prefix="/api/mini-app", tags=["mini-app"])


# Error response model
class ErrorResponse(BaseModel):
    """Standard error response."""

    error: str
    detail: str | None = None

    model_config = ConfigDict(from_attributes=True)


# Response schemas
class UserContextResponse(BaseModel):
    """Response schema for user context (used in /init and /user-context)."""

    user_id: int  # Target user's internal ID
    name: str  # Target user's name
    account_id: int  # Target user's account ID
    roles: list[str]  # Target user's roles (e.g., ["investor", "owner", "stakeholder"])

    model_config = ConfigDict(from_attributes=True)


class UserListItemResponse(BaseModel):
    """Response schema for a single user in the users list."""

    user_id: int
    name: str

    model_config = ConfigDict(from_attributes=True)


class InitResponse(BaseModel):
    """Response schema for /init endpoint."""

    # Root-level auth info (for registered users)
    name: str  # Authenticated user's display name
    is_administrator: bool = False
    representative_id: int | None = None  # Authenticated user's representative_id

    # Nested user context (target user data)
    user_context: UserContextResponse | None = None

    # Users list for admin dropdown (admin only)
    users: list[UserListItemResponse] | None = None

    # Static configuration (only in /init)
    stakeholder_url: str | None = None
    photo_gallery_url: str | None = None

    model_config = ConfigDict(from_attributes=True)


async def _init_build_response(
    session: AsyncSession,
    authenticated_user: User,
    target_user: User,
) -> InitResponse:
    """Build the InitResponse for authenticated user.

    Args:
        session: Database session
        authenticated_user: The user making the request
        target_user: The user whose data is being accessed (may differ if admin/representation)

    Returns:
        InitResponse with authenticated and target user context
    """
    user_service = UserService(session)

    # Build user context for target user
    user_context = await _build_user_context_data(session, target_user)

    # Get users list for admin dropdown (admin only)
    users_list: list[UserListItemResponse] | None = None
    if authenticated_user.is_administrator:
        all_users = await user_service.get_all_users()
        users_list = [
            UserListItemResponse(
                user_id=u.id,
                name=u.name,
            )
            for u in all_users
        ]

    # Static configuration
    photo_gallery_url = os.getenv("PHOTO_GALLERY_URL")
    stakeholder_url = (
        os.getenv("STAKEHOLDER_SHARES_URL")
        if (authenticated_user.is_owner or authenticated_user.is_administrator)
        else None
    )

    return InitResponse(
        name=authenticated_user.name,
        is_administrator=authenticated_user.is_administrator,
        representative_id=authenticated_user.representative_id,
        user_context=user_context,
        users=users_list,
        stakeholder_url=stakeholder_url,
        photo_gallery_url=photo_gallery_url,
    )


class PropertyResponse(BaseModel):
    """Response schema for a single property."""

    id: int
    property_name: str
    type: str
    share_weight: str | None  # Formatted decimal as string for display
    is_ready: bool
    is_for_tenant: bool
    photo_link: str | None
    sale_price: int | None  # Integer price value
    main_property_id: int | None  # ID of parent property if this is an additional property

    model_config = ConfigDict(from_attributes=True)


class PropertiesResponse(BaseModel):
    """Response schema for properties list endpoint."""

    properties: list[PropertyResponse]
    total_count: int

    model_config = ConfigDict(from_attributes=True)


# ---------------------------------------------------------------------------
# Invest / Auction endpoints
# ---------------------------------------------------------------------------


class AuctionBidResponse(BaseModel):
    bid_id: int
    amount: int
    created_at: str

    model_config = ConfigDict(from_attributes=True)


class AuctionPropertyResponse(BaseModel):
    property_id: int
    property_name: str
    property_type: str
    photo_link: str | None = None
    sale_price: int | None = None
    max_active_bid: int
    min_next_bid: int
    my_active_bids: list[AuctionBidResponse]
    share_weight: str | None = None
    main_property_id: int | None = None
    is_ready: bool
    is_for_tenant: bool

    model_config = ConfigDict(from_attributes=True)


class AuctionJournalEntryResponse(BaseModel):
    timestamp: str
    action: str
    property_id: int
    property_name: str
    amount: int
    status: str
    bidder_name: str | None = None

    model_config = ConfigDict(from_attributes=True)


class AuctionOverviewResponse(BaseModel):
    target_sum: int
    collectable_sum: int
    properties: list[AuctionPropertyResponse]
    journal: list[AuctionJournalEntryResponse]

    model_config = ConfigDict(from_attributes=True)


class AuctionPlaceBidRequest(BaseModel):
    property_id: int
    amount: int


class AuctionCancelBidRequest(BaseModel):
    bid_id: int


class OkResponse(BaseModel):
    ok: bool = True

    model_config = ConfigDict(from_attributes=True)


async def _send_bid_notifications(
    session: AsyncSession,
    auction_property: Property,
    target_user: User,
    bid_amount: int,
    previous_bidder: User | None = None,
    previous_bid_amount: int | None = None,
) -> None:
    """Send notifications for new auction bid.

    Args:
        session: Database session
        auction_property: Property the bid was placed on
        target_user: User who placed the bid
        bid_amount: Amount of the new bid
        previous_bidder: Previous max bidder (if any)
        previous_bid_amount: Previous max bid amount (if any)
    """
    bot_app = get_bot_app()  # type: ignore[misc]
    if not bot_app:
        return

    try:
        notifier = NotificationService(bot_app)

        # 1. Notify admin about new bid
        admin_stmt = select(User).where(
            User.is_administrator.is_(True),
            User.telegram_id.isnot(None),
        )
        admin_user = (await session.execute(admin_stmt)).scalar_one_or_none()
        if admin_user and admin_user.telegram_id:
            admin_text = t(
                "msg_new_auction_bid",
                property_name=auction_property.property_name,
                bidder_name=target_user.name,
                amount=format_currency(bid_amount),
            )
            await notifier.send_message(
                chat_id=str(admin_user.telegram_id),
                text=admin_text,
            )

        # 2. Notify previous max bidder if outbid
        if previous_bidder and previous_bidder.telegram_id:
            outbid_text = t(
                "msg_outbid",
                property_name=auction_property.property_name,
                new_amount=format_currency(bid_amount),
                old_amount=format_currency(previous_bid_amount) if previous_bid_amount else "?",
            )
            await notifier.send_message(
                chat_id=str(previous_bidder.telegram_id),
                text=outbid_text,
            )
    except Exception:
        logger.exception("Error sending auction bid notifications")


async def _send_bid_cancel_notification(
    session: AsyncSession,
    auction_property: Property,
    target_user: User,
    bid_amount: int,
) -> None:
    """Send notification for bid cancellation.

    Args:
        session: Database session
        auction_property: Property the bid was cancelled on
        target_user: User who cancelled the bid
        bid_amount: Amount of the cancelled bid
    """
    bot_app = get_bot_app()  # type: ignore[misc]
    if not bot_app:
        return

    try:
        notifier = NotificationService(bot_app)

        admin_stmt = select(User).where(
            User.is_administrator.is_(True),
            User.telegram_id.isnot(None),
        )
        admin_user = (await session.execute(admin_stmt)).scalar_one_or_none()
        if admin_user and admin_user.telegram_id:
            cancel_text = t(
                "msg_auction_bid_canceled",
                property_name=auction_property.property_name,
                bidder_name=target_user.name,
                amount=format_currency(bid_amount),
            )
            await notifier.send_message(
                chat_id=str(admin_user.telegram_id),
                text=cancel_text,
            )
    except Exception:
        logger.exception("Error sending bid cancellation notification")


async def _build_user_context_data(
    session: AsyncSession,
    target_user: User,
) -> UserContextResponse:
    """Build UserContextResponse for a target user.

    This helper is shared by /init and /user-context endpoints.

    Args:
        session: Database session
        target_user: The user to build context for (may be authenticated, represented, or admin-selected)

    Returns:
        UserContextResponse with target user's data
    """
    # Get active roles for target user
    roles = UserStatusService.get_active_roles(target_user)

    # Get target user's account ID
    account_stmt = select(Account).where(Account.user_id == target_user.id)
    account_result = await session.execute(account_stmt)
    target_account = account_result.scalar_one_or_none()

    if not target_account:
        raise HTTPException(status_code=500, detail="Account not found for user")

    return UserContextResponse(
        user_id=target_user.id,
        name=target_user.name,
        account_id=target_account.id,
        roles=roles,
    )


@router.post("/init", response_model=InitResponse)
async def init(
    selected_user_id: int | None = None,
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
    authorization: str | None = Header(None, alias="Authorization"),  # noqa: B008
    body: dict[str, Any] | None = Body(None),  # noqa: B008
) -> InitResponse:
    """
    Initialize Mini App and verify user registration status.

    Returns user access status, menu configuration, and full user context
    for registered users, or access denied message for non-registered users.

    For admins, also returns the list of all users for context switching dropdown.

    Args:
        session: Database session
        selected_user_id: User ID selected by admin (for context switching on page load)

    Returns:
        InitResponse with:
        - Root-level auth info (name, is_administrator, representative_id)
        - Nested user_context for target user (authenticated, represented, or admin-selected)
        - users list for admins only
        - Static config (photo_gallery_url, stakeholder_url)

    Raises:
        401: Invalid Telegram signature
        500: Server error
    """
    start_time = time.time()
    try:
        # Verify Telegram auth and extract telegram_id
        telegram_id = await verify_telegram_auth(session, authorization, body=body)

        # Get authenticated user (checks is_active)
        authenticated_user = await get_authenticated_user(session, telegram_id)

        # Authorize and resolve target user (admin context switching or representation)
        auth_context = await authorize_user_context_access(
            session, authenticated_user, selected_user_id=selected_user_id
        )

        # Build and return init response
        response = await _init_build_response(
            session, auth_context.authenticated_user, auth_context.target_user
        )

        _log_debug(
            "init",
            start_time,
            telegram_id,
            authenticated_user,
            target_user_id=getattr(auth_context.target_user, "id", "?"),
            is_admin=getattr(authenticated_user, "is_administrator", False),
        )
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/mini-app/init: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server error") from e


@router.post("/user-context", response_model=UserContextResponse)
async def get_user_context(
    selected_user_id: int,
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
    authorization: str | None = Header(None, alias="Authorization"),  # noqa: B008
    body: dict[str, Any] | None = Body(None),  # noqa: B008
) -> UserContextResponse:
    """
    Get user context for admin context switching.

    This endpoint is for administrators only and is used when switching
    between users in the admin dropdown. Returns the selected user's context.

    Args:
        session: Database session
        selected_user_id: User ID to get context for (required)

    Returns:
        UserContextResponse with target user's data

    Raises:
        401: Invalid Telegram signature or not an administrator
        404: Selected user not found
        500: Server error
    """
    start_time = time.time()
    try:
        # Verify Telegram auth and extract telegram_id
        telegram_id = await verify_telegram_auth(session, authorization, body=body)

        # Get authenticated user (checks is_active)
        authenticated_user = await get_authenticated_user(session, telegram_id)

        # Authorize admin-only access
        await authorize_user_context_access(
            session, authenticated_user, required_role="is_administrator"
        )

        # Get the selected user
        target_user = await session.get(User, selected_user_id)
        if not target_user:
            raise HTTPException(status_code=404, detail="Selected user not found")

        # Build and return user context for selected user
        response = await _build_user_context_data(session, target_user)

        _log_debug(
            "user_context",
            start_time,
            telegram_id,
            authenticated_user,
            target_user_id=selected_user_id,
        )
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/mini-app/user-context: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server error") from e


@router.post("/properties", response_model=PropertiesResponse)
async def get_properties(
    selected_user_id: int | None = None,
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
    authorization: str | None = Header(None, alias="Authorization"),  # noqa: B008
    body: dict[str, Any] | None = Body(None),  # noqa: B008
) -> PropertiesResponse:
    """
    Get properties for owner or represented owner.

    Returns properties owned by the authenticated user if they are an owner,
    or properties of the user they represent if applicable.
    Uses context switching: if admin selects a user, returns their properties.

    Args:
        session: Database session
        selected_user_id: User ID selected by admin for context switching

    Returns:
        PropertiesResponse with list of properties and total count

    Raises:
        401: Invalid Telegram signature or not an owner
        404: Selected user not found (admin context switch)
        500: Server error
    """
    start_time = time.time()
    try:
        # Verify Telegram auth and extract telegram_id
        telegram_id = await verify_telegram_auth(session, authorization, body=body)

        # Get authenticated user (checks is_active)
        authenticated_user = await get_authenticated_user(session, telegram_id)

        # Authorize user context access with is_owner role requirement
        auth_context = await authorize_user_context_access(
            session, authenticated_user, required_role="is_owner", selected_user_id=selected_user_id
        )

        # Fetch properties for target user
        from src.models.property import Property

        stmt = (
            select(Property)
            .where(
                Property.owner_id == auth_context.target_user.id,
                Property.is_active == True,  # noqa: E712
            )
            .order_by(Property.id)
        )

        result = await session.execute(stmt)
        properties = result.scalars().all()

        # Format response
        property_responses: list[PropertyResponse] = []
        for prop in properties:
            property_responses.append(
                PropertyResponse(
                    id=prop.id,
                    property_name=prop.property_name,
                    type=prop.type,
                    share_weight=str(prop.share_weight) if prop.share_weight else None,
                    is_ready=prop.is_ready,
                    is_for_tenant=prop.is_for_tenant,
                    photo_link=prop.photo_link,
                    sale_price=prop.sale_price,
                    main_property_id=prop.main_property_id,
                )
            )

        response = PropertiesResponse(
            properties=property_responses,
            total_count=len(property_responses),
        )

        _log_debug(
            "properties",
            start_time,
            telegram_id,
            authenticated_user,
            target_user_id=getattr(auth_context.target_user, "id", "?"),
            count=len(property_responses),
        )
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/mini-app/properties: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server error") from e


@router.post("/auction", response_model=AuctionOverviewResponse)
async def get_auction_overview(
    account_id: int | None = None,
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
    authorization: str | None = Header(None, alias="Authorization"),  # noqa: B008
    body: dict[str, Any] | None = Body(None),  # noqa: B008
) -> AuctionOverviewResponse:
    """Get auction overview for investors.

    Returns:
    - target_sum: sum of sale_price for all properties in auction (integer price value)
    - collectable_sum: sum of max active bids per property
    - properties: list of auction properties with max bid + my active bids
    - journal: bid/cancel events sorted by timestamp desc (admin includes bidder_name)

    Representation: Admin/staff can view auction for any account by passing account_id.
    User with representative_id can view auction for represented user's account.
    """
    start_time = time.time()
    try:
        telegram_id = await verify_telegram_auth(session, authorization, body=body)
        authenticated_user = await get_authenticated_user(session, telegram_id)

        # Resolve target user via account_id (for representation)
        if account_id:
            account = await authorize_account_access(session, authenticated_user, account_id)
            if not account.user_id:
                raise HTTPException(status_code=400, detail="ACCOUNT_HAS_NO_USER")
            target_user = await session.get(User, account.user_id)
            if not target_user:
                raise HTTPException(status_code=404, detail="USER_NOT_FOUND")
            is_representing = authenticated_user.id != target_user.id
        else:
            target_user = authenticated_user
            is_representing = False

        auction_service = AuctionService(session)
        totals, props, journal = await auction_service.list_auction(target_user)

        response = AuctionOverviewResponse(
            target_sum=totals.target_sum,
            collectable_sum=totals.collectable_sum,
            properties=[
                AuctionPropertyResponse(
                    property_id=p.property_id,
                    property_name=p.property_name,
                    property_type=p.property_type,
                    share_weight=p.share_weight,
                    photo_link=p.photo_link,
                    sale_price=p.sale_price,
                    main_property_id=p.main_property_id,
                    is_ready=p.is_ready,
                    is_for_tenant=p.is_for_tenant,
                    max_active_bid=p.max_active_bid,
                    min_next_bid=p.min_next_bid,
                    my_active_bids=[
                        AuctionBidResponse(
                            bid_id=b.bid_id,
                            amount=b.amount,
                            created_at=b.created_at.isoformat(),
                        )
                        for b in p.my_active_bids
                    ],
                )
                for p in props
            ],
            journal=[
                AuctionJournalEntryResponse(
                    timestamp=e.timestamp.isoformat(),
                    action=e.action,
                    property_id=e.property_id,
                    property_name=e.property_name,
                    amount=e.amount,
                    status=e.status,
                    bidder_name=e.bidder_name,
                )
                for e in journal
            ],
        )

        _log_debug(
            "auction",
            start_time,
            telegram_id,
            authenticated_user,
            account_id=account_id,
            is_representing=is_representing,
            property_count=len(response.properties),
            journal_count=len(response.journal),
        )
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/mini-app/auction: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server error") from e


@router.post("/auction/bid", response_model=OkResponse)
async def place_auction_bid(
    payload: AuctionPlaceBidRequest,
    account_id: int | None = None,
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
    authorization: str | None = Header(None, alias="Authorization"),  # noqa: B008
) -> OkResponse:
    """Place a new auction bid (investors only).

    Representation: Admin/staff can place bid for any account by passing account_id.
    """
    start_time = time.time()
    try:
        telegram_id = await verify_telegram_auth(session, authorization, body=None)
        authenticated_user = await get_authenticated_user(session, telegram_id)

        # Resolve target user via account_id (for representation)
        if account_id:
            account = await authorize_account_access(session, authenticated_user, account_id)
            if not account.user_id:
                raise HTTPException(status_code=400, detail="ACCOUNT_HAS_NO_USER")
            target_user = await session.get(User, account.user_id)
            if not target_user:
                raise HTTPException(status_code=404, detail="USER_NOT_FOUND")
            is_representing = authenticated_user.id != target_user.id
        else:
            target_user = authenticated_user
            is_representing = False

        # Query previous max bidder before placing new bid (for outbid notification)
        previous_max_bid_stmt = (
            select(AuctionBid)
            .where(
                AuctionBid.property_id == payload.property_id,
                AuctionBid.status == "active",
            )
            .order_by(AuctionBid.amount.desc())
            .limit(1)
        )
        previous_max_bid = (await session.execute(previous_max_bid_stmt)).scalar_one_or_none()
        previous_bidder: User | None = None
        if previous_max_bid:
            previous_bidder = await session.get(User, previous_max_bid.investor_id)

        # Get property for notifications
        auction_property = await session.get(Property, payload.property_id)

        auction_service = AuctionService(session)
        await auction_service.place_bid(
            target_user,
            property_id=payload.property_id,
            amount=payload.amount,
        )

        # Audit log for bid placement (if representing)
        if is_representing:
            await AuditService.log(
                session,
                entity_type="auction_bid",
                entity_id=payload.property_id,
                action="place_bid_on_behalf",
                actor_id=authenticated_user.id,
                changes={
                    "investor_id": target_user.id,
                    "property_id": payload.property_id,
                    "amount": payload.amount,
                },
            )

        await session.commit()

        # Send notifications
        if auction_property:
            await _send_bid_notifications(
                session,
                auction_property,
                target_user,
                payload.amount,
                previous_bidder,
                previous_max_bid.amount if previous_max_bid else None,
            )

        _log_debug(
            "auction_bid",
            start_time,
            telegram_id,
            authenticated_user,
            account_id=account_id,
            is_representing=is_representing,
            property_id=payload.property_id,
            amount=payload.amount,
        )
        return OkResponse(ok=True)

    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in /api/mini-app/auction/bid: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server error") from e


@router.post("/auction/bid/cancel", response_model=OkResponse)
async def cancel_auction_bid(
    payload: AuctionCancelBidRequest,
    account_id: int | None = None,
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
    authorization: str | None = Header(None, alias="Authorization"),  # noqa: B008
) -> OkResponse:
    """Cancel an existing active bid (investor can cancel own bids).

    Representation: Admin/staff can cancel bid for any account by passing account_id.
    """
    start_time = time.time()
    try:
        telegram_id = await verify_telegram_auth(session, authorization, body=None)
        authenticated_user = await get_authenticated_user(session, telegram_id)

        # Resolve target user via account_id (for representation)
        if account_id:
            account = await authorize_account_access(session, authenticated_user, account_id)
            if not account.user_id:
                raise HTTPException(status_code=400, detail="ACCOUNT_HAS_NO_USER")
            target_user = await session.get(User, account.user_id)
            if not target_user:
                raise HTTPException(status_code=404, detail="USER_NOT_FOUND")
            is_representing = authenticated_user.id != target_user.id
        else:
            target_user = authenticated_user
            is_representing = False

        # Get bid details before canceling (for notification)
        bid = await session.get(AuctionBid, payload.bid_id)
        if bid:
            auction_property = await session.get(Property, bid.property_id)
        else:
            auction_property = None

        auction_service = AuctionService(session)
        await auction_service.cancel_bid(target_user, bid_id=payload.bid_id)

        # Audit log for bid cancellation (if representing)
        if is_representing:
            await AuditService.log(
                session,
                entity_type="auction_bid",
                entity_id=payload.bid_id,
                action="cancel_bid_on_behalf",
                actor_id=authenticated_user.id,
                changes={
                    "investor_id": target_user.id,
                    "bid_id": payload.bid_id,
                },
            )

        await session.commit()

        # Send notification to admin
        if auction_property and bid:
            await _send_bid_cancel_notification(
                session,
                auction_property,
                target_user,
                bid.amount,
            )

        _log_debug(
            "auction_cancel",
            start_time,
            telegram_id,
            authenticated_user,
            account_id=account_id,
            is_representing=is_representing,
            bid_id=payload.bid_id,
        )
        return OkResponse(ok=True)

    except HTTPException:
        raise
    except Exception as e:
        await session.rollback()
        logger.error(f"Error in /api/mini-app/auction/bid/cancel: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server error") from e


# Helper functions for bills endpoint
def _build_electricity_reading_subqueries() -> tuple[Any, Any]:
    """Build start and end reading subqueries for electricity bills.

    Returns:
        Tuple of (start_reading_alias, end_reading_alias) scalar subqueries
    """
    from src.models.bill import Bill
    from src.models.electricity_reading import ElectricityReading
    from src.models.service_period import ServicePeriod

    start_reading_alias = (
        select(ElectricityReading.reading_value)
        .where(
            (ElectricityReading.property_id == Bill.property_id)
            & (ElectricityReading.reading_date <= ServicePeriod.start_date)
        )
        .order_by(ElectricityReading.reading_date.desc())
        .limit(1)
        .correlate(Bill, ServicePeriod)
        .scalar_subquery()
    )

    end_reading_alias = (
        select(ElectricityReading.reading_value)
        .where(
            (ElectricityReading.property_id == Bill.property_id)
            & (ElectricityReading.reading_date <= ServicePeriod.end_date)
        )
        .order_by(ElectricityReading.reading_date.desc())
        .limit(1)
        .correlate(Bill, ServicePeriod)
        .scalar_subquery()
    )

    return start_reading_alias, end_reading_alias


def _format_bill_response(
    bill: Any,
    service_period: Any,
    property_obj: Any,
    start_reading: Any,
    end_reading: Any,
) -> dict[str, Any]:
    """Format bill data into response dict.

    Args:
        bill: Bill model instance
        service_period: ServicePeriod model instance
        property_obj: Property model instance or None
        start_reading: Start meter reading value or None
        end_reading: End meter reading value or None

    Returns:
        Formatted bill response dictionary
    """
    consumption = None
    if start_reading is not None and end_reading is not None and end_reading >= start_reading:
        consumption = end_reading - start_reading

    return {
        "period_name": service_period.name,
        "period_start_date": service_period.start_date.isoformat(),
        "period_end_date": service_period.end_date.isoformat(),
        "property_name": property_obj.property_name if property_obj else None,
        "property_type": property_obj.type if property_obj else None,
        "comment": bill.comment,
        "bill_amount": float(bill.bill_amount),
        "bill_type": bill.bill_type.value,
        "start_reading": start_reading,
        "end_reading": end_reading,
        "consumption": consumption,
    }


# Transaction Response Models
class TransactionResponse(BaseModel):
    """Response model for a single transaction."""

    model_config = ConfigDict(from_attributes=True)

    from_account_id: int
    """Account ID the transaction is from."""

    from_ac_name: str
    """Account name the transaction is from."""

    to_account_id: int
    """Account ID the transaction is to."""

    to_ac_name: str
    """Account name the transaction is to."""

    amount: float
    """Transaction amount."""

    date: str
    """Transaction date in ISO format."""

    description: str | None = None
    """Optional transaction description."""


class TransactionsResponse(BaseModel):
    """Response for transactions list."""

    transactions: list[TransactionResponse]


class ElectricityBillResponse(BaseModel):
    """Response schema for a single electricity bill."""

    period_name: str
    """Service period name (e.g., '2024-Q4')."""

    period_start_date: str
    """Period start date in ISO format."""

    period_end_date: str
    """Period end date in ISO format."""

    property_name: str | None = None
    """Property name if bill is for a property."""

    property_type: str | None = None
    """Property type if bill is for a property."""

    start_reading: float | None = None
    """Meter reading at period start (electricity bills only)."""

    end_reading: float | None = None
    """Meter reading at period end (electricity bills only)."""

    consumption: float | None = None
    """Electricity consumption (end - start) (electricity bills only)."""

    bill_amount: float
    """Bill amount in rubles."""

    bill_type: str | None = None
    """Bill type: electricity, shared_electricity, conservation, main."""

    comment: str | None = None
    """Optional comment (e.g., property name when property not found)."""

    model_config = ConfigDict(from_attributes=True)


class BillsResponse(BaseModel):
    """Response for bills list (all bill types: electricity, shared electricity, conservation, main)."""

    bills: list[ElectricityBillResponse]


@router.post("/transactions", response_model=TransactionsResponse)
async def get_transactions(
    account_id: int,
    scope: str = "all",
    authorization: str | None = Header(None),  # noqa: B008
    x_telegram_init_data: str | None = Header(None),  # noqa: B008
    body: dict[str, Any] | None = Body(None),  # noqa: B008
    db: AsyncSession = Depends(get_async_session),  # noqa: B008
) -> TransactionsResponse:
    """Get list of transactions for a specific account.

    Authorization: Admin can access any account, owners can access ORGANIZATION accounts
    and their own personal accounts, staff can access their own accounts.

    Args:
        account_id: Account ID to fetch transactions for (required).
        scope: Filter scope - 'personal' returns only account's transactions,
               'all' (default) returns all organization transactions visible to account.

    Returns:
        TransactionsResponse with filtered transactions.

    Raises:
        400: Missing or invalid account_id
        401: Invalid Telegram signature or unauthorized account access
        404: Account not found
        500: Server error
    """
    start_time = time.time()
    try:
        # Verify Telegram auth and extract telegram_id
        telegram_id = await verify_telegram_auth(db, authorization, x_telegram_init_data, body)

        # Get authenticated user (checks is_active)
        authenticated_user = await get_authenticated_user(db, telegram_id)

        # Authorize account access
        await authorize_account_access(db, authenticated_user, account_id)

        from_account_alias = (
            select(Account.name.label("from_ac_name"))
            .where(Account.id == Transaction.from_account_id)
            .correlate(Transaction)
            .scalar_subquery()
        )

        to_account_alias = (
            select(Account.name.label("to_ac_name"))
            .where(Account.id == Transaction.to_account_id)
            .correlate(Transaction)
            .scalar_subquery()
        )

        where_clause = []
        if scope == "personal":
            where_clause = [
                (Transaction.from_account_id == account_id)
                | (Transaction.to_account_id == account_id)
            ]

        trans_stmt = select(
            Transaction.from_account_id,
            from_account_alias.label("from_ac_name"),
            Transaction.to_account_id,
            to_account_alias.label("to_ac_name"),
            Transaction.amount,
            Transaction.transaction_date,
            Transaction.description,
        ).order_by(Transaction.transaction_date.desc())

        if where_clause:
            trans_stmt = trans_stmt.where(*where_clause)

        result = await db.execute(trans_stmt)
        transactions_data = result.all()

        transactions_list_data = [
            TransactionResponse(
                from_account_id=row[0],
                from_ac_name=row[1] or "Unknown",
                to_account_id=row[2],
                to_ac_name=row[3] or "Unknown",
                amount=float(row[4]),
                date=row[5].isoformat() if row[5] else "",
                description=row[6],
            )
            for row in transactions_data
        ]

        response = TransactionsResponse(transactions=transactions_list_data)

        _log_debug(
            "transactions",
            start_time,
            telegram_id,
            authenticated_user,
            account_id=account_id,
            scope=scope,
            count=len(transactions_list_data),
        )
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/mini-app/transactions: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server error") from e


@router.post("/bills", response_model=BillsResponse)
async def get_bills(
    account_id: int,
    authorization: str | None = Header(None),  # noqa: B008
    x_telegram_init_data: str | None = Header(None),  # noqa: B008
    body: dict[str, Any] | None = Body(None),  # noqa: B008
    db: AsyncSession = Depends(get_async_session),  # noqa: B008
) -> BillsResponse:
    """Get list of all bills for a specific account.

    Returns bills for the account itself and for properties owned by the account owner.
    Authorization: Admin can access any account, owners can access ORGANIZATION accounts
    and their own personal accounts, staff can access their own accounts.

    Args:
        account_id: Account ID to fetch bills for (required).

    Returns:
        BillsResponse with list of bills sorted by period (most recent first).

    Raises:
        400: Missing or invalid account_id
        401: Invalid Telegram signature or unauthorized account access
        404: Account not found
        500: Server error
    """
    from src.models.bill import Bill
    from src.models.property import Property
    from src.models.service_period import ServicePeriod

    start_time = time.time()
    try:
        # Verify Telegram auth and extract telegram_id
        telegram_id = await verify_telegram_auth(db, authorization, x_telegram_init_data, body)

        # Get authenticated user (checks is_active)
        authenticated_user = await get_authenticated_user(db, telegram_id)

        # Authorize account access
        account = await authorize_account_access(db, authenticated_user, account_id)

        # Get properties owned by account's user
        user_property_ids = []
        if account.user_id:
            user_properties_stmt = select(Property.id).where(Property.owner_id == account.user_id)
            result = await db.execute(user_properties_stmt)
            user_property_ids = [row[0] for row in result.all()]

        # Build reading subqueries
        start_reading_alias, end_reading_alias = _build_electricity_reading_subqueries()

        # Build where clause for account and owner properties
        where_clause = [Bill.account_id == account_id]
        if user_property_ids:
            from sqlalchemy import or_

            where_clause = [
                or_(Bill.account_id == account_id, Bill.property_id.in_(user_property_ids))
            ]

        # Query bills
        bills_stmt = (
            select(Bill, ServicePeriod, Property, start_reading_alias, end_reading_alias)
            .join(ServicePeriod, Bill.service_period_id == ServicePeriod.id)
            .outerjoin(Property, Bill.property_id == Property.id)
            .where(*where_clause)
            .order_by(ServicePeriod.start_date.desc())
        )

        result = await db.execute(bills_stmt)
        bills_data = result.all()

        # Build response list
        bills_response: list[ElectricityBillResponse] = []
        for row_data in bills_data:
            bill = row_data[0]
            service_period = row_data[1]
            property_obj = row_data[2]

            start_reading = None
            end_reading = None
            if bill.bill_type.value == "electricity":
                start_reading = float(row_data[3]) if row_data[3] else None
                end_reading = float(row_data[4]) if row_data[4] else None

            bill_response = _format_bill_response(
                bill, service_period, property_obj, start_reading, end_reading
            )
            bills_response.append(ElectricityBillResponse(**bill_response))

        response = BillsResponse(bills=bills_response)

        _log_debug(
            "bills",
            start_time,
            telegram_id,
            authenticated_user,
            account_id=account_id,
            count=len(bills_response),
        )
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/mini-app/bills: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server error") from e


# Balance Response Models
class AccountResponse(BaseModel):
    """Response schema for single user account endpoint."""

    balance: float  # Raw balance value
    invert_for_display: bool = False  # True for OWNER accounts (display negated value)
    suggested_payment: int | None = None  # Suggested owner payment when debt exists

    model_config = ConfigDict(from_attributes=True)


class AccountItem(BaseModel):
    """Response schema for a single account's info item."""

    account_id: int  # Account ID for navigation
    account_name: str
    account_type: str  # 'owner', 'staff', or 'organization'
    balance: float
    invert_for_display: bool = False  # True for OWNER accounts
    representative_id: int | None = (
        None  # User's representative_id (if set, they represent someone)
    )

    model_config = ConfigDict(from_attributes=True)


class AccountsResponse(BaseModel):
    """Response schema for accounts endpoint."""

    accounts: list[AccountItem]

    model_config = ConfigDict(from_attributes=True)


async def _fetch_primary_organization_account(session: AsyncSession) -> Account | None:
    """Return the primary organization account (used for owner debt suggestions)."""

    stmt = (
        select(Account)
        .where(Account.account_type == AccountType.ORGANIZATION)
        .order_by(Account.id)
        .limit(1)
    )
    result = await session.execute(stmt)
    return result.scalar_one_or_none()


async def _calculate_owner_suggested_payment(
    session: AsyncSession,
    transaction_service: TransactionService,
    owner_account: Account,
    balance: float,
    invert_for_display: bool,
) -> int | None:
    """Return a suggested payment amount for owners with debt."""

    if not invert_for_display or balance <= 0:
        return None

    org_account = await _fetch_primary_organization_account(session)
    if not org_account:
        return None

    suggested = await transaction_service.calculate_suggested_amount(owner_account, org_account)
    return suggested if suggested > 0 else None


@router.post("/account", response_model=AccountResponse)
async def get_account(
    account_id: int,
    authorization: str | None = Header(None),  # noqa: B008
    x_telegram_init_data: str | None = Header(None),  # noqa: B008
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
    body: dict[str, Any] | None = Body(None),  # noqa: B008
) -> AccountResponse:
    """Get balance (payments - bills) for a specific account.

    Authorization: Admin can access any account, owners can access ORGANIZATION accounts
    and their own personal accounts, staff can access their own accounts.

    Args:
        account_id: Account ID to calculate balance for (required).

    Returns:
        AccountResponse with balance value (positive = credit, negative = debt).

    Raises:
        400: Missing or invalid account_id
        401: Invalid Telegram signature or unauthorized account access
        404: Account not found
        500: Server error
    """
    start_time = time.time()
    try:
        # Verify Telegram auth and extract telegram_id
        telegram_id = await verify_telegram_auth(session, authorization, x_telegram_init_data, body)

        # Get authenticated user (checks is_active)
        authenticated_user = await get_authenticated_user(session, telegram_id)

        # Authorize account access and reuse the account object
        account = await authorize_account_access(session, authenticated_user, account_id)

        # Calculate balance using service
        from src.services.balance_service import BalanceCalculationService

        balance_service = BalanceCalculationService(session)
        result = await balance_service.calculate_account_balance_with_display(account.id)

        transaction_service = TransactionService(session)
        suggested_payment = await _calculate_owner_suggested_payment(
            session,
            transaction_service,
            account,
            result.balance,
            result.invert_for_display,
        )

        response = AccountResponse(
            balance=result.balance,
            invert_for_display=result.invert_for_display,
            suggested_payment=suggested_payment,
        )

        _log_debug(
            "account",
            start_time,
            telegram_id,
            authenticated_user,
            account_id=account_id,
            balance=f"{result.balance:.2f}",
            suggested_payment=suggested_payment or "-",
        )
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/mini-app/account: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server error") from e


@router.post("/accounts", response_model=AccountsResponse)
async def get_accounts(
    authorization: str | None = Header(None),  # noqa: B008
    x_telegram_init_data: str | None = Header(None),  # noqa: B008
    session: AsyncSession = Depends(get_async_session),  # noqa: B008
    body: dict[str, Any] | None = Body(None),  # noqa: B008
) -> AccountsResponse:
    """Get all accounts with balances for authorized users.

    Available to: admin, owner, and staff roles.
    All authorized users can view all account balances - info is not role-restricted.

    Returns:
        AccountsResponse with all accounts and their balances

    Raises:
        401: Invalid Telegram signature or user lacks required role
        500: Server error
    """
    start_time = time.time()
    try:
        # Verify Telegram auth and extract telegram_id
        telegram_id = await verify_telegram_auth(session, authorization, x_telegram_init_data, body)

        # Get authenticated user (checks is_active)
        authenticated_user = await get_authenticated_user(session, telegram_id)

        # Authorize role-based access (admin, owner, or staff)
        await authorize_account_access_for_roles(
            session, authenticated_user, 1, ["is_administrator", "is_owner", "is_staff"]
        )

        # Get all accounts for balance display
        from sqlalchemy.orm import selectinload

        from src.services.balance_service import BalanceCalculationService

        stmt = select(Account).options(selectinload(Account.user))  # type: ignore[arg-type]
        result = await session.execute(stmt)
        accounts = result.scalars().all()

        accounts_list: list[AccountItem] = []
        balance_service = BalanceCalculationService(session)

        for account in accounts:
            # Calculate balance using service with display info
            result = await balance_service.calculate_account_balance_with_display(account.id)

            # Handle account_type as either Enum or string
            account_type_str = (
                account.account_type.value
                if hasattr(account.account_type, "value")
                else str(account.account_type)
            )

            # Get representative_id from the user relationship (if account has a user)
            representative_id = account.user.representative_id if account.user else None  # type: ignore[attr-defined]

            accounts_list.append(
                AccountItem(
                    account_id=account.id,
                    account_name=account.name,
                    account_type=account_type_str,
                    balance=result.balance,
                    invert_for_display=result.invert_for_display,
                    representative_id=representative_id,  # type: ignore[arg-type]
                )
            )

        response = AccountsResponse(accounts=accounts_list)

        _log_debug(
            "accounts", start_time, telegram_id, authenticated_user, count=len(accounts_list)
        )
        return response

    except HTTPException:
        raise
    except Exception as e:
        logger.error(f"Error in /api/mini-app/accounts: {e}", exc_info=True)
        raise HTTPException(status_code=500, detail="Server error") from e


__all__ = ["router", "_extract_init_data"]

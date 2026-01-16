"""Contract tests for Invest/Auction Mini App endpoints.

These tests validate endpoint response schemas and basic wiring without relying on a real DB.
We mock auth and service-layer logic and override the DB session dependency.
"""

from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.mini_app import AuctionOverviewResponse
from src.main import app
from src.services import get_async_session
from src.services.auction_service import (
    AuctionBidInfo,
    AuctionJournalEntry,
    AuctionPropertyInfo,
    AuctionTotals,
)


class _DummySession:
    async def commit(self) -> None:  # pragma: no cover
        return None

    async def rollback(self) -> None:  # pragma: no cover
        return None

    async def execute(self, stmt):  # pragma: no cover
        """Mock execute method for database queries."""
        result = MagicMock()
        result.scalar_one_or_none = MagicMock(return_value=None)
        result.scalars = MagicMock(return_value=MagicMock(all=MagicMock(return_value=[])))
        return result

    async def get(self, model, id):  # pragma: no cover
        """Mock get method for entity retrieval."""
        # Return a mock object with property_name for Property model
        if hasattr(model, "__name__") and model.__name__ == "Property":
            mock_obj = MagicMock()
            mock_obj.property_name = "Test Property"
            return mock_obj
        # Return mock User
        if hasattr(model, "__name__") and model.__name__ == "User":
            mock_user = MagicMock()
            mock_user.id = 1
            mock_user.telegram_id = "123"
            return mock_user
        return None


async def _override_get_async_session():
    yield _DummySession()


@pytest.fixture
def client():
    app.dependency_overrides[get_async_session] = _override_get_async_session
    try:
        yield TestClient(app)
    finally:
        app.dependency_overrides.pop(get_async_session, None)


def _mk_user(*, user_id: int = 1, is_investor: bool = True, is_admin: bool = False):
    return SimpleNamespace(
        id=user_id,
        is_investor=is_investor,
        is_administrator=is_admin,
        representative_id=None,
    )


class TestAuctionOverviewEndpoint:
    def test_auction_overview_response_schema_valid(self, client: TestClient):
        user = _mk_user(is_investor=True, is_admin=False)

        totals = AuctionTotals(target_sum=1500, collectable_sum=700)
        props = [
            AuctionPropertyInfo(
                property_id=10,
                property_name="Main House",
                property_type="house",
                share_weight="1.0",
                photo_link=None,
                sale_price=1000,
                main_property_id=None,
                is_ready=True,
                is_for_tenant=False,
                max_active_bid=500,
                min_next_bid=550,
                my_active_bids=[
                    AuctionBidInfo(
                        bid_id=101,
                        amount=500,
                        created_at=datetime(2026, 1, 10, tzinfo=timezone.utc),
                    )
                ],
            ),
            AuctionPropertyInfo(
                property_id=11,
                property_name="Shed",
                property_type="addon",
                share_weight=None,
                photo_link="https://example.com",
                sale_price=500,
                main_property_id=10,
                is_ready=False,
                is_for_tenant=True,
                max_active_bid=200,
                min_next_bid=220,
                my_active_bids=[],
            ),
        ]
        journal = [
            AuctionJournalEntry(
                timestamp=datetime(2026, 1, 11, tzinfo=timezone.utc),
                action="bid",
                property_id=10,
                property_name="Main House",
                amount=500,
                status="active",
                bidder_name=None,
            )
        ]

        with (
            patch(
                "src.api.mini_app.verify_telegram_auth",
                new=AsyncMock(return_value=999),
            ),
            patch(
                "src.api.mini_app.get_authenticated_user",
                new=AsyncMock(return_value=user),
            ),
            patch("src.api.mini_app.AuctionService") as mock_service_cls,
        ):
            service = mock_service_cls.return_value
            service.list_auction = AsyncMock(return_value=(totals, props, journal))

            resp = client.post(
                "/api/mini-app/auction", headers={"Authorization": "tma test"}, json={}
            )
            assert resp.status_code == 200

            # Pydantic validation = schema contract.
            model = AuctionOverviewResponse(**resp.json())
            assert model.target_sum == 1500
            assert model.collectable_sum == 700
            assert len(model.properties) == 2
            assert model.properties[0].property_id == 10
            assert model.properties[1].main_property_id == 10

    def test_auction_overview_admin_can_receive_bidder_name(self, client: TestClient):
        user = _mk_user(is_investor=True, is_admin=True)

        totals = AuctionTotals(target_sum=0, collectable_sum=0)
        props: list[AuctionPropertyInfo] = []
        journal = [
            AuctionJournalEntry(
                timestamp=datetime(2026, 1, 11, tzinfo=timezone.utc),
                action="cancel",
                property_id=10,
                property_name="Main House",
                amount=500,
                status="canceled",
                bidder_name="Alice",
            )
        ]

        with (
            patch(
                "src.api.mini_app.verify_telegram_auth",
                new=AsyncMock(return_value=999),
            ),
            patch(
                "src.api.mini_app.get_authenticated_user",
                new=AsyncMock(return_value=user),
            ),
            patch("src.api.mini_app.AuctionService") as mock_service_cls,
        ):
            service = mock_service_cls.return_value
            service.list_auction = AsyncMock(return_value=(totals, props, journal))

            resp = client.post(
                "/api/mini-app/auction", headers={"Authorization": "tma test"}, json={}
            )
            assert resp.status_code == 200
            data = resp.json()
            assert data["journal"][0]["bidder_name"] == "Alice"


class TestAuctionBidEndpoints:
    def test_place_bid_ok(self, client: TestClient):
        user = _mk_user(is_investor=True)

        with (
            patch(
                "src.api.mini_app.verify_telegram_auth",
                new=AsyncMock(return_value=999),
            ),
            patch(
                "src.api.mini_app.get_authenticated_user",
                new=AsyncMock(return_value=user),
            ),
            patch("src.api.mini_app.AuctionService") as mock_service_cls,
            patch("src.api.mini_app._send_bid_notifications", new=AsyncMock()),
        ):
            service = mock_service_cls.return_value
            service.place_bid = AsyncMock(return_value=None)

            resp = client.post(
                "/api/mini-app/auction/bid",
                headers={"Authorization": "tma test"},
                json={"property_id": 10, "amount": 123},
            )
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
            service.place_bid.assert_awaited()

    def test_cancel_bid_ok(self, client: TestClient):
        user = _mk_user(is_investor=True)

        with (
            patch(
                "src.api.mini_app.verify_telegram_auth",
                new=AsyncMock(return_value=999),
            ),
            patch(
                "src.api.mini_app.get_authenticated_user",
                new=AsyncMock(return_value=user),
            ),
            patch("src.api.mini_app.AuctionService") as mock_service_cls,
            patch("src.api.mini_app._send_bid_cancel_notification", new=AsyncMock()),
        ):
            service = mock_service_cls.return_value
            service.cancel_bid = AsyncMock(return_value=None)

            resp = client.post(
                "/api/mini-app/auction/bid/cancel",
                headers={"Authorization": "tma test"},
                json={"bid_id": 101},
            )
            assert resp.status_code == 200
            assert resp.json() == {"ok": True}
            service.cancel_bid.assert_awaited()

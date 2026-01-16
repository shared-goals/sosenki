"""Contract tests for admin handlers.

T036-T038: Contract tests for admin approval flow.
T047-T049: Contract tests for admin rejection flow (placeholders).
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete

from src.api import webhook as webhook_module
from src.models.access_request import AccessRequest, RequestStatus
from src.models.user import User
from src.services import SessionLocal


@pytest.fixture(autouse=True)
def cleanup_db():
    """Clean up and setup database before and after each test."""
    db = SessionLocal()
    try:
        # Clean up previous test data
        db.execute(delete(AccessRequest))
        db.execute(delete(User))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

    # Setup: Create admin users for tests
    db = SessionLocal()
    try:
        # Admin user 987654321 (used in approval tests)
        admin1 = User(
            telegram_id=987654321,
            name="Test Admin 1",
            is_active=True,
            is_administrator=True,
        )
        db.add(admin1)

        # Admin user 777666555 (used in rejection tests)
        admin2 = User(
            telegram_id=777666555,
            name="Test Admin 2",
            is_active=True,
            is_administrator=True,
        )
        db.add(admin2)
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()

    yield

    db = SessionLocal()
    try:
        db.execute(delete(AccessRequest))
        db.execute(delete(User))
        db.commit()
    except Exception:
        db.rollback()
    finally:
        db.close()


@pytest.fixture
def mock_bot():
    """Create a mock bot with async methods."""
    bot = MagicMock()
    bot.send_message = AsyncMock()
    return bot


@pytest.fixture
def client(mock_bot):
    """FastAPI test client with mocked bot."""
    with patch("telegram.Update.de_json") as mock_de_json:

        def de_json_side_effect(data, bot_instance):
            """Convert dict to Update object."""
            update = MagicMock()
            update.message = None
            update.callback_query = None

            if data and "message" in data:
                update.message = MagicMock()
                update.message.text = data["message"]["text"]
                update.message.from_user = MagicMock()
                update.message.from_user.id = data["message"]["from"]["id"]
                update.message.from_user.first_name = data["message"]["from"].get(
                    "first_name", "Admin"
                )
                update.message.from_user.username = data["message"]["from"].get("username", None)
                # Handle reply_to_message
                update.message.reply_to_message = None
                if "reply_to_message" in data["message"]:
                    rtm = MagicMock()
                    rtm.text = data["message"]["reply_to_message"].get("text", "")
                    update.message.reply_to_message = rtm
                update.message.chat = MagicMock()
                update.message.chat.id = data["message"]["from"]["id"]
                update.message.chat.type = data["message"].get("chat", {}).get("type", "private")
                update.message.reply_text = AsyncMock()

            if data and "callback_query" in data:
                cq_data = data["callback_query"]
                update.callback_query = MagicMock()
                update.callback_query.id = cq_data.get("id", "callback_123")
                update.callback_query.data = cq_data.get("data", "")
                update.callback_query.from_user = MagicMock()
                update.callback_query.from_user.id = cq_data["from"]["id"]
                update.callback_query.from_user.first_name = cq_data["from"].get(
                    "first_name", "Admin"
                )
                update.callback_query.answer = AsyncMock()
                update.callback_query.edit_message_text = AsyncMock()

            if update.message is None and update.callback_query is None:
                return None
            return update

        mock_de_json.side_effect = de_json_side_effect

        mock_app = MagicMock()
        mock_app.bot = mock_bot
        mock_app.process_update = AsyncMock()

        async def process_update_impl(update):
            """Process update through the handler."""
            from src.bot.handlers import handle_admin_response
            from src.bot.handlers.admin_requests import handle_admin_callback

            ctx = MagicMock()
            ctx.application = mock_app
            ctx.bot_data = {}

            # Route to callback handler for callback queries
            if update.callback_query:
                await handle_admin_callback(update, ctx)
            # Route to message handler for text messages
            elif update.message and update.message.text:
                await handle_admin_response(update, ctx)

        mock_app.process_update.side_effect = process_update_impl

        from src.api import bot_context

        bot_context.set_bot_app(mock_app)

        yield TestClient(webhook_module.app)

        bot_context.set_bot_app(None)  # type: ignore[arg-type]


class TestAdminHandlers:
    """Contract tests for admin approval/rejection handlers."""

    def _create_request_in_db(self, client_id: int, message: str = "Help needed"):
        """Helper to create a pending request in database."""
        db = SessionLocal()
        try:
            request = AccessRequest(
                user_telegram_id=str(client_id),
                request_message=message,
                status=RequestStatus.PENDING,
            )
            db.add(request)
            db.commit()
            db.refresh(request)
            return request
        finally:
            db.close()

    def test_admin_approval_handler(self, client, mock_bot):
        """Test admin approval via callback button (T036).

        Contract: POST /webhook/telegram with callback_query "approve:<id>"
        → returns 200, request status updated to approved, client receives welcome message.
        """
        # Setup: Create a pending request
        client_id = 123456789
        admin_id = 987654321
        request = self._create_request_in_db(client_id, "Emergency help")

        # Create Telegram Update for admin approval via callback button
        update_json = {
            "update_id": 100,
            "callback_query": {
                "id": "callback_123",
                "from": {"id": admin_id, "is_bot": False, "first_name": "Admin"},
                "chat_instance": "12345",
                "data": f"approve:{request.id}",
                "message": {
                    "message_id": 50,
                    "date": int(datetime.now(timezone.utc).timestamp()),
                    "chat": {"id": admin_id, "type": "private"},
                    "from": {"id": 777, "is_bot": True},
                    "text": f"Request #{request.id}\n\nTest (ID: {client_id})\n\nMessage:\nEmergency help",
                },
            },
        }

        # Send approval update
        response = client.post("/webhook/telegram", json=update_json)

        # Verify endpoint returns 200
        assert response.status_code == 200
        assert response.json()["ok"] is True

        # Verify request status updated in database
        db = SessionLocal()
        try:
            updated_request = db.query(AccessRequest).filter(AccessRequest.id == request.id).first()
            assert updated_request is not None
            assert updated_request.status == RequestStatus.APPROVED
            # Check that admin_telegram_id is now set
            assert updated_request.admin_telegram_id is not None
        finally:
            db.close()

    def test_approval_with_invalid_request(self, client, mock_bot):
        """Test approval when request doesn't exist (T037).

        Contract: POST /webhook/telegram with callback "approve:99999" when request doesn't exist
        → returns 200, admin receives error "Request not found".
        """
        admin_id = 987654321

        # Create update with approval but no valid request ID
        update_json = {
            "update_id": 101,
            "callback_query": {
                "id": "callback_456",
                "from": {"id": admin_id, "is_bot": False, "first_name": "Admin"},
                "chat_instance": "12345",
                "data": "approve:99999",
                "message": {
                    "message_id": 99,
                    "date": int(datetime.now(timezone.utc).timestamp()),
                    "chat": {"id": admin_id, "type": "private"},
                    "from": {"id": 777, "is_bot": True},
                    "text": "Request #99999\n\nUnknown (ID: 999999)\n\nMessage:\nTest",
                },
            },
        }

        # Send approval update
        response = client.post("/webhook/telegram", json=update_json)

        # Verify endpoint returns 200
        assert response.status_code == 200
        assert response.json()["ok"] is True

        # Verify no requests in database (invalid request, nothing changed)
        db = SessionLocal()
        try:
            all_requests = db.query(AccessRequest).all()
            assert len(all_requests) == 0
        finally:
            db.close()

    def test_admin_rejection_handler(self, client, mock_bot):
        """Test admin rejection via callback button (T047).

        Contract: POST /webhook/telegram with callback_query "reject:<id>"
        → returns 200, request status updated to rejected, client receives rejection message.
        """
        # Setup: Create a pending request
        client_id = 111222333
        admin_id = 987654321
        request = self._create_request_in_db(client_id, "Suspicious request")

        # Create Telegram Update for admin rejection via callback button
        update_json = {
            "update_id": 200,
            "callback_query": {
                "id": "callback_789",
                "from": {"id": admin_id, "is_bot": False, "first_name": "Admin"},
                "chat_instance": "12345",
                "data": f"reject:{request.id}",
                "message": {
                    "message_id": 60,
                    "date": int(datetime.now(timezone.utc).timestamp()),
                    "chat": {"id": admin_id, "type": "private"},
                    "from": {"id": 777, "is_bot": True},
                    "text": f"Request #{request.id}\n\nTest2 (ID: {client_id})\n\nMessage:\nSuspicious request",
                },
            },
        }

        # Send rejection update
        response = client.post("/webhook/telegram", json=update_json)

        # Verify endpoint returns 200
        assert response.status_code == 200
        assert response.json()["ok"] is True

        # Verify request status updated in database
        db = SessionLocal()
        try:
            updated_request = db.query(AccessRequest).filter(AccessRequest.id == request.id).first()
            assert updated_request is not None
            assert updated_request.status == RequestStatus.REJECTED
            assert updated_request.admin_telegram_id == admin_id
        finally:
            db.close()

    def test_rejection_with_invalid_request(self, client, mock_bot):
        """Test rejection when request doesn't exist (T048).

        Contract: POST /webhook/telegram with callback "reject:99999" when request doesn't exist
        → returns 200, admin receives error "Request not found".
        """
        admin_id = 555666777

        # Create update with rejection but no valid request ID
        update_json = {
            "update_id": 201,
            "callback_query": {
                "id": "callback_999",
                "from": {"id": admin_id, "is_bot": False, "first_name": "Admin"},
                "chat_instance": "12345",
                "data": "reject:99999",
                "message": {
                    "message_id": 199,
                    "date": int(datetime.now(timezone.utc).timestamp()),
                    "chat": {"id": admin_id, "type": "private"},
                    "from": {"id": 777, "is_bot": True},
                    "text": "Request #99999\n\nUnknown (ID: 999999)\n\nMessage:\nTest",
                },
            },
        }

        # Send rejection update
        response = client.post("/webhook/telegram", json=update_json)

        # Verify endpoint returns 200
        assert response.status_code == 200
        assert response.json()["ok"] is True

        # Verify no requests in database
        db = SessionLocal()
        try:
            all_requests = db.query(AccessRequest).all()
            assert len(all_requests) == 0
        finally:
            db.close()

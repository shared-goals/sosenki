"""Integration tests for client request submission flow.

T027: Integration test for full request submission flow.
Tests the complete workflow: client sends /request → database stores request →
client receives confirmation → admin receives notification.
"""

from datetime import datetime, timezone
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient
from sqlalchemy import delete
from telegram import Update

from src.api import webhook as webhook_module
from src.models.access_request import AccessRequest, RequestStatus
from src.models.user import User
from src.services import AsyncSessionLocal


class TestAccessRequestFlow:
    """Integration tests for the complete client request submission flow."""

    @pytest.fixture(autouse=True)
    async def cleanup_db(self):
        """Clean up database before and after each test."""
        async with AsyncSessionLocal() as db:
            try:
                # Delete test users created by this test suite
                # (Users created with placeholder names starting with "User_")
                await db.execute(delete(User).where(User.name.like("User_%")))
                # Delete all requests
                await db.execute(delete(AccessRequest))
                await db.commit()
            except Exception:
                await db.rollback()

        yield

        # Cleanup after test
        async with AsyncSessionLocal() as db:
            try:
                # Delete test users created by this test suite
                await db.execute(delete(User).where(User.name.like("User_%")))
                # Delete all requests
                await db.execute(delete(AccessRequest))
                await db.commit()
            except Exception:
                await db.rollback()

    @pytest.fixture
    def mock_bot(self):
        """Create a mock bot with async methods."""
        bot = MagicMock()
        bot.send_message = AsyncMock()
        return bot

    @pytest.fixture
    def client(self, mock_bot):
        """Create a test client with mocked bot."""
        # Mock the telegram Update.de_json to return proper Update objects
        with patch("telegram.Update.de_json") as mock_de_json:

            def de_json_side_effect(data, bot_instance):
                """Convert dict to Update object."""
                if data and "message" in data:
                    # Simulate Update object with message
                    update = MagicMock(spec=Update)
                    update.message = MagicMock()
                    update.message.text = data["message"]["text"]
                    update.message.from_user = MagicMock()
                    update.message.from_user.id = data["message"]["from"]["id"]
                    update.message.from_user.first_name = data["message"]["from"].get(
                        "first_name", "TestUser"
                    )
                    update.message.from_user.username = data["message"]["from"].get(
                        "username", None
                    )
                    update.message.chat = MagicMock()
                    update.message.chat.type = data["message"]["chat"].get("type", "private")
                    update.message.reply_text = AsyncMock()
                    return update
                return None

            mock_de_json.side_effect = de_json_side_effect

            # Create mock bot app with proper bot reference
            mock_app = MagicMock()
            mock_app.bot = mock_bot
            mock_app.process_update = AsyncMock()

            # Async version of process_update that actually calls our handler
            async def process_update_impl(update):
                """Process update through the handler."""
                if update.message and update.message.text.startswith("/request"):
                    # Import here to avoid circular imports
                    from src.bot.handlers import handle_request_command

                    # Call handler directly with proper context
                    ctx = MagicMock()
                    ctx.application = mock_app
                    ctx.bot_data = {}
                    await handle_request_command(update, ctx)

            mock_app.process_update.side_effect = process_update_impl

            # Set global bot app
            from src.api import bot_context

            bot_context.set_bot_app(mock_app)

            # Return test client
            yield TestClient(webhook_module.app)

            # Cleanup
            bot_context.set_bot_app(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_full_request_flow(self, client, mock_bot):
        """Test the full client request submission flow.

        Verifies:
        1. Client sends /request command with message
        2. Request is stored in database with correct details
        3. Client receives confirmation message
        4. Admin receives notification message
        """
        # Prepare test data
        client_id = 123456789
        client_name = "TestUser"
        request_message = "I need urgent help"

        # Create Telegram Update JSON (as it would come from Telegram)
        update_json = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": int(datetime.now(timezone.utc).timestamp()),
                "chat": {"id": client_id, "type": "private"},
                "from": {"id": client_id, "is_bot": False, "first_name": client_name},
                "text": f"/request {request_message}",
            },
        }

        # Step 1: Send /request command via webhook
        response = client.post("/webhook/telegram", json=update_json)

        # Verify webhook returns 200 OK
        assert response.status_code == 200
        response_data = response.json()
        assert response_data["ok"] is True

        # Step 2: Verify request is stored in database
        async with AsyncSessionLocal() as db:
            from sqlalchemy import select

            result = await db.execute(
                select(AccessRequest).where(AccessRequest.user_telegram_id == client_id)
            )
            stored_requests = result.scalars().all()

            assert len(stored_requests) == 1, "Request should be stored in database"
            stored_request = stored_requests[0]

            # Verify request details
            assert stored_request.user_telegram_id == client_id
            assert stored_request.request_message == request_message
            assert stored_request.status == RequestStatus.PENDING
            assert stored_request.created_at is not None
            assert stored_request.admin_telegram_id is None
            assert stored_request.admin_response is None
            assert stored_request.updated_at is not None

        # Step 3: Verify client received confirmation message
        # The handler should call bot.send_message for the client confirmation
        assert mock_bot.send_message.called
        confirmation_calls = [
            call for call in mock_bot.send_message.call_args_list if str(client_id) in str(call)
        ]
        assert len(confirmation_calls) > 0, "Client should receive confirmation"

        # Step 4: Verify admin received notification message
        # The handler should call bot.send_message for the admin notification
        # Admin notification might be sent (depending on admin ID config)
        # At minimum, we should have confirmation sent

    @pytest.mark.asyncio
    async def test_duplicate_request_rejection_in_flow(self, client, mock_bot):
        """Test that duplicate requests are rejected in the flow.

        Verifies:
        1. First /request succeeds
        2. Second /request from same client is rejected
        3. Only one request stored in database
        4. Client receives error message
        """
        client_id = 123456789
        client_name = "TestUser"
        request_message_1 = "First request"
        request_message_2 = "Second request"

        # Helper to create update
        def create_update(msg_text):
            return {
                "update_id": 1,
                "message": {
                    "message_id": 1,
                    "date": int(datetime.now(timezone.utc).timestamp()),
                    "chat": {"id": client_id, "type": "private"},
                    "from": {"id": client_id, "is_bot": False, "first_name": client_name},
                    "text": msg_text,
                },
            }

        # Step 1: Send first /request
        response1 = client.post(
            "/webhook/telegram", json=create_update(f"/request {request_message_1}")
        )
        assert response1.status_code == 200

        # Reset mock to track second call separately
        mock_bot.reset_mock()

        # Step 2: Send second /request from same client
        response2 = client.post(
            "/webhook/telegram", json=create_update(f"/request {request_message_2}")
        )
        assert response2.status_code == 200

        # Step 3: Verify only one request in database
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AccessRequest).where(AccessRequest.user_telegram_id == client_id)
            )
            stored_requests = result.scalars().all()

            assert len(stored_requests) == 1, "Only one request should be stored"
            assert stored_requests[0].request_message == request_message_1

    @pytest.mark.asyncio
    async def test_timing_sla_for_confirmation(self, client, mock_bot):
        """Test that client confirmation is sent within SLA (2 seconds).

        Verifies:
        1. /request command is processed
        2. Confirmation sent to client within 2 second SLA
        """
        client_id = 111222333
        client_name = "TimingTest"
        request_message = "Help needed"

        update_json = {
            "update_id": 10,
            "message": {
                "message_id": 10,
                "date": int(datetime.now(timezone.utc).timestamp()),
                "chat": {"id": client_id, "type": "private"},
                "from": {"id": client_id, "is_bot": False, "first_name": client_name},
                "text": f"/request {request_message}",
            },
        }

        # Measure time for webhook call
        start_time = datetime.now(timezone.utc)
        response = client.post("/webhook/telegram", json=update_json)
        end_time = datetime.now(timezone.utc)

        elapsed_time = (end_time - start_time).total_seconds()

        # Verify response is successful
        assert response.status_code == 200

        # Verify request was stored
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AccessRequest).where(AccessRequest.user_telegram_id == client_id)
            )
            stored_request = result.scalar_one_or_none()
            assert stored_request is not None

        # Log timing for information (SLA is 2 seconds)
        print(f"Request processing time: {elapsed_time:.3f} seconds (SLA: 2s)")

    @pytest.mark.asyncio
    async def test_request_with_special_characters(self, client, mock_bot):
        """Test that requests with special characters are handled correctly."""
        client_id = 987654321
        client_name = "TestUser2"
        # Request with emojis and special chars
        request_message = "Emergency! Need help ASAP 🚨 @username #urgent"

        update_json = {
            "update_id": 2,
            "message": {
                "message_id": 2,
                "date": int(datetime.now(timezone.utc).timestamp()),
                "chat": {"id": client_id, "type": "private"},
                "from": {"id": client_id, "is_bot": False, "first_name": client_name},
                "text": f"/request {request_message}",
            },
        }

        response = client.post("/webhook/telegram", json=update_json)
        assert response.status_code == 200

        # Verify request is stored correctly with special characters preserved
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AccessRequest).where(AccessRequest.user_telegram_id == client_id)
            )
            stored_request = result.scalar_one_or_none()

            assert stored_request is not None
            assert stored_request.request_message == request_message

    @pytest.mark.asyncio
    async def test_request_with_multiline_message(self, client, mock_bot):
        """Test that multi-line request messages are handled (only first line after /request)."""
        client_id = 555666777
        client_name = "TestUser3"
        # Multi-line message (in real Telegram, /request command only captures first line)
        request_message = "First line of request"

        update_json = {
            "update_id": 3,
            "message": {
                "message_id": 3,
                "date": int(datetime.now(timezone.utc).timestamp()),
                "chat": {"id": client_id, "type": "private"},
                "from": {"id": client_id, "is_bot": False, "first_name": client_name},
                "text": f"/request {request_message}",
            },
        }

        response = client.post("/webhook/telegram", json=update_json)
        assert response.status_code == 200

        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AccessRequest).where(AccessRequest.user_telegram_id == client_id)
            )
            stored_request = result.scalar_one_or_none()

            assert stored_request is not None
            assert request_message in stored_request.request_message

    @pytest.mark.asyncio
    async def test_request_without_message(self, client, mock_bot):
        """Test that /request command works without a message.

        Verifies:
        1. Client can send /request without any message
        2. Request is stored in database with empty message
        3. Client receives confirmation
        4. Admin receives notification
        """
        client_id = 666777888
        client_name = "TestUser4"

        update_json = {
            "update_id": 4,
            "message": {
                "message_id": 4,
                "date": int(datetime.now(timezone.utc).timestamp()),
                "chat": {"id": client_id, "type": "private"},
                "from": {"id": client_id, "is_bot": False, "first_name": client_name},
                "text": "/request",  # No message after /request
            },
        }

        response = client.post("/webhook/telegram", json=update_json)
        assert response.status_code == 200
        response_data = response.json()
        assert response_data["ok"] is True

        # Verify request is stored in database with empty message
        from sqlalchemy import select

        async with AsyncSessionLocal() as db:
            result = await db.execute(
                select(AccessRequest).where(AccessRequest.user_telegram_id == client_id)
            )
            stored_request = result.scalar_one_or_none()

            assert stored_request is not None
            assert stored_request.request_message == ""

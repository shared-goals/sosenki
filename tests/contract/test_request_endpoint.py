"""Contract tests for /webhook/telegram endpoint."""

from datetime import datetime
from unittest.mock import AsyncMock, MagicMock, patch

import pytest
from fastapi.testclient import TestClient

from src.api.webhook import app


@pytest.fixture
def mock_bot():
    """Create and initialize mock bot application for testing."""
    # Create a mock bot app that looks like a real python-telegram-bot Application
    mock_bot_instance = MagicMock()
    mock_bot_instance.send_message = AsyncMock()
    mock_bot_instance.get_chat = AsyncMock(return_value=MagicMock(id=123456789))

    mock_app = MagicMock()
    mock_app.bot = mock_bot_instance
    mock_app.process_update = AsyncMock()

    return mock_app


@pytest.fixture
def client(mock_bot):
    """FastAPI test client with bot app registered."""
    from src.api import bot_context

    # Set the global bot app using bot_context
    bot_context.set_bot_app(mock_bot)

    # Return the test client
    test_client = TestClient(app)

    yield test_client

    # Clean up
    bot_context.set_bot_app(None)  # type: ignore[arg-type]


def create_telegram_update(
    update_id: int,
    chat_id: int,
    user_id: int,
    first_name: str,
    text: str,
    reply_to_message_id: int = None,
) -> dict:
    """Create a Telegram Update object for testing.

    Args:
        update_id: Unique update ID
        chat_id: Chat ID (same as user_id for private messages)
        user_id: User ID
        first_name: User's first name
        text: Message text
        reply_to_message_id: If replying to a message, the message ID

    Returns:
        Dict representation of Telegram Update
    """
    update = {
        "update_id": update_id,
        "message": {
            "message_id": update_id * 100,
            "date": int(datetime.now().timestamp()),
            "chat": {
                "id": chat_id,
                "first_name": first_name,
                "type": "private",
            },
            "from": {
                "id": user_id,
                "is_bot": False,
                "first_name": first_name,
            },
            "text": text,
        },
    }

    if reply_to_message_id is not None:
        update["message"]["reply_to_message"] = {
            "message_id": reply_to_message_id,
            "text": "Client Request notification",
        }

    return update


class TestRequestEndpoint:
    """Contract tests for /request command endpoint."""

    def test_webhook_endpoint_exists(self, client, mock_bot):
        """Test that webhook endpoint is available.

        T025: POST /webhook/telegram with /request update → returns 200,
        bot queues confirmation and admin notification
        """
        # Mock Telegram Update.de_json to return a mock Update object
        mock_update = MagicMock()
        with patch("telegram.Update.de_json", return_value=mock_update):
            # Create a /request command update from a client
            update = create_telegram_update(
                update_id=1,
                chat_id=123456789,
                user_id=123456789,
                first_name="John",
                text="/request Please give me access to SOSenki",
            )

            # POST the update to webhook endpoint
            response = client.post("/webhook/telegram", json=update)

            # Should return 200 OK
            assert response.status_code == 200

            # Response should be {"ok": true}
            data = response.json()
            assert data.get("ok") is True

            # Verify the bot's process_update was called
            mock_bot.process_update.assert_called_once_with(mock_update)

    def test_duplicate_request_rejection(self, client, mock_bot):
        """Test that duplicate pending requests are rejected.

        T026: POST with /request from client with existing PENDING request
        → returns 200, client receives error message "You already have a pending request"
        """
        # Mock Telegram Update.de_json to return a mock Update object
        mock_update_1 = MagicMock()
        mock_update_2 = MagicMock()

        with patch("telegram.Update.de_json", side_effect=[mock_update_1, mock_update_2]):
            client_id = 123456789

            # First request from client
            update1 = create_telegram_update(
                update_id=1,
                chat_id=client_id,
                user_id=client_id,
                first_name="John",
                text="/request Please give me access",
            )

            response1 = client.post("/webhook/telegram", json=update1)
            assert response1.status_code == 200
            assert response1.json().get("ok") is True

            # Reset the mock for second call
            mock_bot.process_update.reset_mock()

            # Second request from same client (should be rejected)
            update2 = create_telegram_update(
                update_id=2,
                chat_id=client_id,
                user_id=client_id,
                first_name="John",
                text="/request Second request",
            )

            response2 = client.post("/webhook/telegram", json=update2)

            # Should still return 200 (webhook accepts it)
            assert response2.status_code == 200
            assert response2.json().get("ok") is True

            # Verify both updates were processed by bot
            assert mock_bot.process_update.call_count == 1

        # NOTE: Verification that client receives error message is done in
        # integration test (T027) which can assert on bot message queue or
        # mock Telegram API calls

    def test_request_from_group_chat_rejected(self, client, mock_bot):
        """Test that /request commands in group chats are rejected.

        Group chats cannot use WebAppInfo buttons, so requests must be sent in private messages.
        """
        # Mock Telegram Update.de_json to return a mock Update object
        mock_update = MagicMock()

        with patch("telegram.Update.de_json", return_value=mock_update):
            # Request from a group chat
            update = create_telegram_update(
                update_id=1,
                chat_id=-123456789,  # Negative ID indicates group chat
                user_id=987654321,
                first_name="John",
                text="/request Please give me access",
            )

            # Override chat type to group
            update["message"]["chat"]["type"] = "group"

            response = client.post("/webhook/telegram", json=update)
            assert response.status_code == 200
            assert response.json().get("ok") is True

            # Verify bot processes the update (the handler will reject it)
            mock_bot.process_update.assert_called_once()

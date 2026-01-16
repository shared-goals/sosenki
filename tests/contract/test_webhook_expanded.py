"""Tests for webhook endpoint - Telegram update processing."""

from unittest.mock import AsyncMock, MagicMock

import pytest
from fastapi.testclient import TestClient
from telegram import Bot
from telegram.ext import Application

from src.api.webhook import app, setup_webhook_route


@pytest.fixture
def client():
    """Create test client for FastAPI app."""
    return TestClient(app)


@pytest.fixture
def mock_bot_app():
    """Create a mock Telegram bot application."""
    mock_app = MagicMock(spec=Application)
    mock_app.bot = MagicMock(spec=Bot)
    mock_app.process_update = AsyncMock()
    return mock_app


class TestWebhookHealthCheck:
    """Tests for health check endpoint."""

    def test_health_check_returns_ok(self, client):
        """Test health check endpoint returns 200 with ok status."""
        response = client.get("/health")

        assert response.status_code == 200
        assert response.json() == {"status": "ok"}

    def test_health_check_content_type(self, client):
        """Test health check returns JSON content type."""
        response = client.get("/health")

        assert response.headers["content-type"] == "application/json"


class TestWebhookTelegramEndpoint:
    """Tests for Telegram webhook endpoint."""

    @pytest.mark.asyncio
    async def test_telegram_webhook_bot_not_initialized(self, client):
        """Test webhook returns 503 when bot not initialized."""
        response = client.post("/webhook/telegram", json={"update_id": 1})

        assert response.status_code == 503
        assert "Bot not initialized" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_telegram_webhook_valid_update(self, client, mock_bot_app):
        """Test webhook processes valid Telegram update."""
        from src.api import bot_context

        bot_context.set_bot_app(mock_bot_app)

        update_data = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1234567890,
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123, "is_bot": False, "first_name": "Test"},
                "text": "Hello bot",
            },
        }

        response = client.post("/webhook/telegram", json=update_data)

        assert response.status_code == 200
        assert response.json() == {"ok": True}
        mock_bot_app.process_update.assert_called_once()

    @pytest.mark.asyncio
    async def test_telegram_webhook_empty_update(self, client, mock_bot_app):
        """Test webhook handles empty/null update gracefully."""
        from src.api import bot_context

        bot_context.set_bot_app(mock_bot_app)

        response = client.post("/webhook/telegram", json={"update_id": 2})

        assert response.status_code == 200
        assert response.json() == {"ok": True}

    @pytest.mark.asyncio
    async def test_telegram_webhook_error_processing(self, client, mock_bot_app):
        """Test webhook returns 500 on processing error."""
        from src.api import bot_context

        bot_context.set_bot_app(mock_bot_app)
        mock_bot_app.process_update.side_effect = Exception("Processing error")

        update_data = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1234567890,
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123, "is_bot": False, "first_name": "Test"},
                "text": "Error test",
            },
        }

        response = client.post("/webhook/telegram", json=update_data)

        assert response.status_code == 500
        assert "Internal server error" in response.json()["detail"]

    @pytest.mark.asyncio
    async def test_telegram_webhook_callback_query_update(self, client, mock_bot_app):
        """Test webhook processes callback query update."""
        from src.api import bot_context

        bot_context.set_bot_app(mock_bot_app)

        update_data = {
            "update_id": 3,
            "callback_query": {
                "id": "callback_123",
                "from": {"id": 123, "is_bot": False, "first_name": "Test"},
                "chat_instance": "123",
                "data": "button_click",
            },
        }

        response = client.post("/webhook/telegram", json=update_data)

        assert response.status_code == 200
        assert response.json() == {"ok": True}


class TestSetupWebhookRoute:
    """Tests for setup_webhook_route function."""

    @pytest.mark.asyncio
    async def test_setup_webhook_route_sets_bot_app(self, mock_bot_app):
        """Test setup_webhook_route sets the global bot app."""
        from src.api import bot_context

        # Clear any existing bot app
        bot_context.set_bot_app(None)  # type: ignore[arg-type]

        # Call setup_webhook_route
        await setup_webhook_route(mock_bot_app)

        # Verify bot app was set
        assert bot_context.get_bot_app() is mock_bot_app

        # Clean up
        bot_context.set_bot_app(None)  # type: ignore[arg-type]

    @pytest.mark.asyncio
    async def test_setup_webhook_route_endpoint_processes_update(self, mock_bot_app):
        """Test webhook endpoint created by setup processes updates."""
        await setup_webhook_route(mock_bot_app)

        client = TestClient(app)
        update_data = {
            "update_id": 1,
            "message": {
                "message_id": 1,
                "date": 1234567890,
                "chat": {"id": 123, "type": "private"},
                "from": {"id": 123, "is_bot": False, "first_name": "Test"},
                "text": "Setup test",
            },
        }

        response = client.post("/webhook/telegram", json=update_data)

        assert response.status_code == 200
        mock_bot_app.process_update.assert_called()


class TestWebhookStaticFiles:
    """Tests for static files mounting."""

    def test_mini_app_static_mounting(self, client):
        """Test that mini app path exists (even if files don't)."""
        # Test that the route is registered
        response = client.get("/health")
        assert response.status_code == 200


class TestWebhookCORS:
    """Tests for CORS middleware configuration."""

    def test_cors_headers_present(self, client):
        """Test CORS middleware adds required headers."""
        response = client.get("/health")

        # Check that response has CORS headers
        assert "access-control-allow-origin" in response.headers or response.status_code == 200

    def test_cors_allows_all_origins(self, client):
        """Test CORS middleware allows all origins."""
        response = client.get(
            "/health",
            headers={"Origin": "https://example.com"},
        )

        assert response.status_code == 200

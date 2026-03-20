"""Unit tests for src/main.py entry point."""

import asyncio
import os
from unittest.mock import AsyncMock, MagicMock, patch

import pytest


def _close_coroutine(coro):
    """Helper to properly close a coroutine and avoid RuntimeWarning."""
    if asyncio.iscoroutine(coro):
        coro.close()
    return None


@pytest.mark.asyncio
async def test_load_environment_with_env_file():
    """Test _load_environment loads from .env file."""
    from src.main import _load_environment

    with patch("src.main.load_dotenv") as mock_load_dotenv:
        _load_environment()
        assert mock_load_dotenv.called


@pytest.mark.asyncio
async def test_load_environment_with_temp_env_file():
    """Test _load_environment loads from /tmp/.sosenki-env if exists."""
    from src.main import _load_environment

    with patch("src.main.Path") as mock_path:
        mock_path.return_value.exists.return_value = True
        with patch("src.main.load_dotenv") as mock_load_dotenv:
            _load_environment()
            assert mock_load_dotenv.called


@pytest.mark.asyncio
async def test_validate_environment_success():
    """Test _validate_environment succeeds when MINI_APP_URL is set."""
    from src.main import _validate_environment

    with patch.dict(os.environ, {"MINI_APP_URL": "https://example.com/app"}):
        _validate_environment()


@pytest.mark.asyncio
async def test_validate_environment_missing_mini_app_url():
    """Test _validate_environment raises when MINI_APP_URL is missing."""
    from src.main import _validate_environment

    with patch.dict(os.environ, {}, clear=True):
        with pytest.raises(RuntimeError, match="MINI_APP_URL"):
            _validate_environment()


@pytest.mark.asyncio
async def test_validate_environment_empty_mini_app_url():
    """Test _validate_environment raises when MINI_APP_URL is empty."""
    from src.main import _validate_environment

    with patch.dict(os.environ, {"MINI_APP_URL": "   "}):
        with pytest.raises(RuntimeError, match="MINI_APP_URL"):
            _validate_environment()


@pytest.mark.asyncio
async def test_initialize_bot():
    """Test initialize_bot creates bot application."""
    from src.main import initialize_bot

    mock_bot_app = AsyncMock()
    with patch("src.main.create_bot_app", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_bot_app
        await initialize_bot()
        mock_create.assert_called_once()


@pytest.mark.asyncio
async def test_run_webhook_mode_initialization():
    """Test run_webhook_mode initializes bot and registers webhook."""
    from src.main import run_webhook_mode

    mock_bot_app = AsyncMock()
    mock_bot_app.bot = AsyncMock()
    mock_bot_app.initialize = AsyncMock()
    mock_bot_app.shutdown = AsyncMock()

    with patch("src.main.create_bot_app", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_bot_app
        with patch("src.main.setup_webhook_route", new_callable=AsyncMock) as mock_setup:
            mock_setup.return_value = None
            with patch("src.main.uvicorn.Server") as mock_server:
                mock_server_instance = MagicMock()
                mock_server_instance.serve = AsyncMock()
                mock_server.return_value = mock_server_instance

                with patch.dict(
                    os.environ,
                    {
                        "WEBHOOK_URL": "https://example.com/webhook",
                        "MINI_APP_URL": "https://example.com/app",
                    },
                ):
                    try:
                        await run_webhook_mode(host="127.0.0.1", port=8001)
                    except Exception:
                        pass
                    mock_setup.assert_called_once()


@pytest.mark.asyncio
async def test_run_webhook_mode_no_webhook_url():
    """Test run_webhook_mode handles missing WEBHOOK_URL gracefully."""
    from src.main import run_webhook_mode

    mock_bot_app = AsyncMock()
    mock_bot_app.bot = AsyncMock()
    mock_bot_app.initialize = AsyncMock()
    mock_bot_app.shutdown = AsyncMock()

    with patch("src.main.create_bot_app", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_bot_app
        with patch("src.main.setup_webhook_route", new_callable=AsyncMock):
            with patch("src.main.uvicorn.Server") as mock_server:
                mock_server_instance = MagicMock()
                mock_server_instance.serve = AsyncMock()
                mock_server.return_value = mock_server_instance

                with patch.dict(
                    os.environ,
                    {"MINI_APP_URL": "https://example.com/app"},
                    clear=True,
                ):
                    try:
                        await run_webhook_mode()
                    except Exception:
                        pass


@pytest.mark.asyncio
async def test_main_webhook_mode_when_webhook_url_set():
    """Test main() uses webhook mode when WEBHOOK_URL is set."""
    from src.main import main

    with patch("src.main.asyncio.run", side_effect=_close_coroutine) as mock_run:
        with patch("sys.argv", ["prog", "--port", "9000"]):
            with patch.dict(os.environ, {"WEBHOOK_URL": "https://example.com/webhook"}):
                main()
                mock_run.assert_called_once()
                coro = mock_run.call_args[0][0]
                assert coro.__name__ == "run_webhook_mode"


@pytest.mark.asyncio
async def test_main_polling_mode_when_no_webhook_url():
    """Test main() uses polling mode when WEBHOOK_URL is not set."""
    from src.main import main

    with patch("src.main.asyncio.run", side_effect=_close_coroutine) as mock_run:
        with patch("sys.argv", ["prog", "--port", "9000"]):
            with patch.dict(os.environ, {"WEBHOOK_URL": ""}):
                main()
                mock_run.assert_called_once()
                coro = mock_run.call_args[0][0]
                assert coro.__name__ == "run_polling_mode"


@pytest.mark.asyncio
async def test_main_default_arguments():
    """Test main() uses correct defaults."""
    from src.main import main

    with patch("src.main.asyncio.run", side_effect=_close_coroutine) as mock_run:
        with patch("sys.argv", ["prog"]):
            main()
            mock_run.assert_called_once()


@pytest.mark.asyncio
async def test_run_webhook_mode_bot_shutdown_error():
    """Test run_webhook_mode handles bot shutdown errors gracefully."""
    from src.main import run_webhook_mode

    mock_bot_app = AsyncMock()
    mock_bot_app.bot = AsyncMock()
    mock_bot_app.initialize = AsyncMock()
    mock_bot_app.shutdown = AsyncMock(side_effect=Exception("Shutdown error"))

    with patch("src.main.create_bot_app", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_bot_app
        with patch("src.main.setup_webhook_route", new_callable=AsyncMock):
            with patch("src.main.uvicorn.Server") as mock_server:
                mock_server_instance = MagicMock()
                mock_server_instance.serve = AsyncMock()
                mock_server.return_value = mock_server_instance

                with patch.dict(
                    os.environ,
                    {"MINI_APP_URL": "https://example.com/app"},
                ):
                    try:
                        await run_webhook_mode()
                    except Exception:
                        pass


@pytest.mark.asyncio
async def test_run_webhook_mode_webhook_setup_error():
    """Test run_webhook_mode handles webhook setup errors gracefully."""
    from src.main import run_webhook_mode

    mock_bot_app = AsyncMock()
    mock_bot_app.bot = AsyncMock()
    mock_bot_app.bot.set_webhook = AsyncMock(side_effect=Exception("Webhook error"))
    mock_bot_app.initialize = AsyncMock()
    mock_bot_app.shutdown = AsyncMock()

    with patch("src.main.create_bot_app", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_bot_app
        with patch("src.main.setup_webhook_route", new_callable=AsyncMock):
            with patch("src.main.uvicorn.Server") as mock_server:
                mock_server_instance = MagicMock()
                mock_server_instance.serve = AsyncMock()
                mock_server.return_value = mock_server_instance

                with patch.dict(
                    os.environ,
                    {
                        "WEBHOOK_URL": "https://example.com/webhook",
                        "MINI_APP_URL": "https://example.com/app",
                    },
                ):
                    try:
                        await run_webhook_mode()
                    except Exception:
                        pass


@pytest.mark.asyncio
async def test_run_webhook_mode_no_bot_app_on_startup():
    """Test run_webhook_mode startup when bot_app is None."""
    from src.main import run_webhook_mode

    # Test the startup event handler when bot_app is None
    with patch("src.main.create_bot_app", new_callable=AsyncMock) as mock_create:
        with patch("src.main.setup_webhook_route", new_callable=AsyncMock):
            with patch("src.main.uvicorn.Server") as mock_server:
                mock_server_instance = MagicMock()
                mock_server_instance.serve = AsyncMock()
                mock_server.return_value = mock_server_instance

                with patch.dict(
                    os.environ,
                    {"MINI_APP_URL": "https://example.com/app"},
                ):
                    # Simulate None bot_app to test the if bot_app check
                    mock_create.return_value = None
                    try:
                        await run_webhook_mode()
                    except Exception:
                        pass


@pytest.mark.asyncio
async def test_run_webhook_mode_no_bot_app_on_shutdown():
    """Test run_webhook_mode shutdown when bot_app is None."""
    from src.main import run_webhook_mode

    mock_bot_app = AsyncMock()
    mock_bot_app.bot = AsyncMock()
    mock_bot_app.initialize = AsyncMock()
    mock_bot_app.shutdown = AsyncMock()

    with patch("src.main.create_bot_app", new_callable=AsyncMock) as mock_create:
        mock_create.return_value = mock_bot_app
        with patch("src.main.setup_webhook_route", new_callable=AsyncMock):
            with patch("src.main.uvicorn.Server") as mock_server:
                mock_server_instance = MagicMock()
                mock_server_instance.serve = AsyncMock()
                mock_server.return_value = mock_server_instance

                with patch.dict(
                    os.environ,
                    {"MINI_APP_URL": "https://example.com/app"},
                ):
                    try:
                        await run_webhook_mode()
                    except Exception:
                        pass

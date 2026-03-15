"""Telegram bot configuration from environment variables and database."""

import logging
from typing import Optional

from pydantic import ConfigDict
from pydantic_settings import BaseSettings

logger = logging.getLogger(__name__)


class BotConfig(BaseSettings):
    """Bot configuration loaded from environment variables.

    Pydantic automatically loads values from:
    1. OS environment variables (at instantiation time)
    2. .env file (if env_file is set)

    IMPORTANT: Instantiate AFTER environment variables are loaded.
    This is handled by the lazy loader below.
    """

    model_config = ConfigDict(
        env_file=".env",
        case_sensitive=False,
        extra="ignore",  # Allow extra fields in .env file
    )

    telegram_bot_token: str
    telegram_bot_name: str

    # Mini App configuration (002-welcome-mini-app)
    # Will be set by setup-environment.sh at runtime
    telegram_mini_app_id: str
    mini_app_url: str = ""
    all_proxy: str = ""

    # Admin telegram ID is now loaded from database via get_admin_telegram_id()
    admin_telegram_id: str = ""  # Will be set dynamically when needed

    def validate(self) -> None:
        """Validate required configuration is present."""
        if not self.telegram_bot_token:
            raise ValueError("TELEGRAM_BOT_TOKEN environment variable is required")


# Lazy loader to ensure environment is loaded before instantiation
_bot_config_instance: Optional[BotConfig] = None


def get_bot_config() -> BotConfig:
    """Get or create bot config instance.

    This is lazy-loaded to ensure environment variables are available
    (including dynamic ones from setup-environment.sh written to /tmp/.sosenki-env)
    """
    global _bot_config_instance
    if _bot_config_instance is None:
        _bot_config_instance = BotConfig()
        _bot_config_instance.validate()
    return _bot_config_instance


# For backward compatibility, provide direct access
# This will be instantiated when first accessed (lazy loading)
class _BotConfigProxy:
    """Proxy to provide attribute access while lazy-loading the config."""

    def __getattr__(self, name: str):
        return getattr(get_bot_config(), name)


bot_config = _BotConfigProxy()

__all__ = ["BotConfig", "bot_config", "get_bot_config"]

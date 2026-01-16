"""FastAPI webhook endpoint for Telegram updates."""

import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.middleware.cors import CORSMiddleware
from fastapi.staticfiles import StaticFiles
from telegram import Update
from telegram.ext import Application

from src.api.bot_context import get_bot_app, set_bot_app
from src.api.mcp_server import mcp_http_app
from src.api.mini_app import router as mini_app_router

logger = logging.getLogger(__name__)

# FastAPI instance using MCP's lifespan directly (handles DB lifecycle)
app = FastAPI(
    title="SOSenki Bot",
    description="Client Request Approval Workflow - Telegram Bot",
    version="0.1.0",
    lifespan=mcp_http_app.lifespan,
)

# Add CORS middleware for Mini App (required for iPhone/iOS requests)
# Desktop version works without explicit CORS, but iOS requires CORS headers
app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],  # Allow all origins (safe for Telegram Mini App)
    allow_credentials=True,
    allow_methods=["*"],
    allow_headers=["*"],
)

# Mount static files for Mini App (002-welcome-mini-app)
static_path = Path(__file__).parent.parent / "static" / "mini_app"
if static_path.exists():
    app.mount("/mini-app", StaticFiles(directory=str(static_path), html=True), name="mini-app")
    logger.info(f"Mounted Mini App static files from {static_path}")

# Include Mini App API router first (higher priority)
app.include_router(mini_app_router)

# Mount MCP HTTP app at /mcp path using FastMCP
app.mount("/mcp", mcp_http_app, name="mcp")


async def setup_webhook_route(bot_app: Application) -> None:
    """Set up the bot application for webhook processing.

    Args:
        bot_app: Telegram bot Application instance
    """
    set_bot_app(bot_app)


# Register health check endpoint
@app.get("/health")
async def health_check() -> dict:
    """Health check endpoint for monitoring."""
    return {"status": "ok"}


# Register Telegram webhook endpoint
@app.post("/webhook/telegram")
async def telegram_webhook(update: dict) -> dict:
    """Receive Telegram updates and dispatch to bot handlers.

    Args:
        update: Telegram Update object (as JSON)

    Returns:
        {"ok": True} response as per Telegram webhook protocol
    """
    bot_app = get_bot_app()
    if not bot_app:
        logger.error("Bot application not initialized")
        raise HTTPException(status_code=503, detail="Bot not initialized")

    try:
        telegram_update = Update.de_json(update, bot_app.bot)
        if telegram_update:
            # Log incoming update with key identifiers
            user_id = telegram_update.effective_user.id if telegram_update.effective_user else None
            chat_id = telegram_update.effective_chat.id if telegram_update.effective_chat else None
            update_type = (
                telegram_update.effective_message.text[:50]
                if telegram_update.effective_message and telegram_update.effective_message.text
                else "callback/other"
            )
            logger.debug(
                "webhook.telegram: update_id=%d user_id=%s chat_id=%s type=%s",
                telegram_update.update_id,
                user_id,
                chat_id,
                update_type,
            )
            await bot_app.process_update(telegram_update)
        return {"ok": True}
    except Exception as e:
        logger.error("Error processing update: %s", e, exc_info=True)
        raise HTTPException(status_code=500, detail="Internal server error") from e


__all__ = ["app", "setup_webhook_route"]

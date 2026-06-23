"""Logging configuration and utilities for bot and mini app server.

Provides dual output (stdout + file) with configurable level via LOG_LEVEL env var.
Default: INFO. Set LOG_LEVEL=WARNING for production, DEBUG for verbose output.
Suitable for both real-time developer feedback and audit trails.
"""

import logging
import os
import sys
from pathlib import Path


class HttpxTelegramPollingFilter(logging.Filter):
    """Downgrade Telegram long-polling request logs to DEBUG.

    This keeps periodic getUpdates noise out of INFO logs while preserving
    visibility when LOG_LEVEL=DEBUG.
    """

    def filter(self, record: logging.LogRecord) -> bool:
        if (
            record.name == "httpx"
            and record.levelno == logging.INFO
            and "getUpdates" in record.getMessage()
        ):
            record.levelno = logging.DEBUG
            record.levelname = logging.getLevelName(logging.DEBUG)
        return True

# Map string level names to logging constants
LOG_LEVEL_MAP = {
    "DEBUG": logging.DEBUG,
    "INFO": logging.INFO,
    "WARNING": logging.WARNING,
    "ERROR": logging.ERROR,
    "CRITICAL": logging.CRITICAL,
}


def get_log_level() -> int:
    """Get logging level from LOG_LEVEL environment variable.

    Returns:
        Logging level constant (default: INFO)
    """
    level_str = os.getenv("LOG_LEVEL", "INFO").upper()
    return LOG_LEVEL_MAP.get(level_str, logging.INFO)


def setup_server_logging(log_file: str = "logs/server.log") -> None:
    """
    Configure root logger for bot + mini app server.

    Args:
        log_file: Path to log file (default: logs/server.log)

    Behavior:
        - Sets up all loggers to output to both stdout and file
        - ISO format timestamps for consistency
        - Suitable for both real-time debugging and audit trails
    """
    # Create logs directory if it doesn't exist
    log_path = Path(log_file)
    log_path.parent.mkdir(parents=True, exist_ok=True)

    # ISO format: [YYYY-MM-DD HH:MM:SS]
    formatter = logging.Formatter(
        fmt="[%(asctime)s] %(name)s - %(levelname)s - %(message)s",
        datefmt="%Y-%m-%d %H:%M:%S",
    )

    # Get configured log level from environment
    log_level = get_log_level()

    # Get root logger
    root_logger = logging.getLogger()
    root_logger.setLevel(log_level)

    # Keep Telegram polling request noise out of INFO logs.
    logging.getLogger("httpx").addFilter(HttpxTelegramPollingFilter())

    # Remove any existing handlers to avoid duplicates
    root_logger.handlers.clear()

    # Handler 1: stdout (console)
    stdout_handler = logging.StreamHandler(sys.stdout)
    stdout_handler.setLevel(log_level)
    stdout_handler.setFormatter(formatter)
    root_logger.addHandler(stdout_handler)

    # Handler 2: file (logs/server.log)
    file_handler = logging.FileHandler(log_path)
    file_handler.setLevel(log_level)
    file_handler.setFormatter(formatter)
    root_logger.addHandler(file_handler)

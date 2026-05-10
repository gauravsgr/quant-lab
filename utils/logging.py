"""Logging configuration for the trading system.

Sets up two loguru sinks:
    stderr  - colorized, human-readable output at INFO level
    file    - serialized JSON at DEBUG level, rotating at 50 MB, retained 30 days

Structured JSON logs enable downstream parsing (e.g., grep by ticker or signal_type).
Call configure_logger() once at application startup before any logger.* calls.
"""
import sys
from loguru import logger


def configure_logger(log_path: str = "logs/trading.log") -> None:
    """Configure loguru with colorized stderr and serialized file output.

    Removes any existing handlers before adding new ones, so this is
    safe to call multiple times without duplicating output.

    Args:
        log_path: Path to the rotating JSON log file. The parent directory
            must exist or loguru will raise an error on first write.
    """
    logger.remove()
    logger.add(
        sys.stderr,
        level="INFO",
        colorize=True,
        format="<green>{time:YYYY-MM-DD HH:mm:ss}</green> | <level>{level}</level> | {message}",
    )
    logger.add(
        log_path,
        rotation="50 MB",
        retention="30 days",
        serialize=True,
        level="DEBUG",
    )

"""Application-wide settings loaded from environment variables.

All required variables must be present in .env (gitignored). See .env.example
for the full list of keys and their expected formats.

Typical usage:
    from config.settings import load_settings
    settings = load_settings()
    print(settings.trading_mode)  # "paper" or "live"
"""
import os
from dataclasses import dataclass
from dotenv import load_dotenv

load_dotenv()


@dataclass(frozen=True)
class Settings:
    """Immutable application configuration.

    Attributes:
        trading_mode: "paper" or "live". Controls which Alpaca endpoint is used.
        alpaca_api_key: Alpaca API key ID.
        alpaca_secret_key: Alpaca API secret key.
        alpaca_base_url: Alpaca REST base URL. Defaults to the paper endpoint.
        adanos_api_key: Adanos Market Sentiment API key.
        slack_webhook_url: Slack incoming webhook URL for trade alerts.
        db_path: Path to the SQLite database file.
        agent_timezone: IANA timezone name for APScheduler cron jobs.
        require_approval: When True, signals are logged and notified but not executed.
    """
    trading_mode: str
    alpaca_api_key: str
    alpaca_secret_key: str
    alpaca_base_url: str
    adanos_api_key: str
    slack_webhook_url: str
    db_path: str
    agent_timezone: str
    require_approval: bool


def load_settings() -> Settings:
    """Read environment variables and return a validated Settings instance.

    Returns:
        A frozen Settings dataclass with all configuration values.

    Raises:
        EnvironmentError: If a required environment variable is missing or empty.
    """
    return Settings(
        trading_mode=_require("TRADING_MODE"),
        alpaca_api_key=_require("ALPACA_API_KEY"),
        alpaca_secret_key=_require("ALPACA_SECRET_KEY"),
        alpaca_base_url=os.getenv("ALPACA_BASE_URL", "https://paper-api.alpaca.markets/v2"),
        adanos_api_key=_require("ADANOS_API_KEY"),
        slack_webhook_url=_require("SLACK_WEBHOOK_URL"),
        db_path=os.getenv("DB_PATH", "trading_system.db"),
        agent_timezone=os.getenv("AGENT_TIMEZONE", "America/New_York"),
        require_approval=os.getenv("REQUIRE_APPROVAL", "false").lower() == "true",
    )


def _require(key: str) -> str:
    """Return the value of an environment variable or raise if it is missing.

    Args:
        key: The environment variable name.

    Returns:
        The non-empty string value of the variable.

    Raises:
        EnvironmentError: If the variable is not set or is an empty string.
    """
    val = os.getenv(key)
    if not val:
        raise EnvironmentError(f"Required environment variable not set: {key}")
    return val

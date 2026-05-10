"""Adanos Market Sentiment API client with monthly budget enforcement.

Adanos provides social sentiment scores and buzz rankings across Reddit,
StockTwits, and Twitter/X. The free tier allows 250 calls/month; this
client enforces a hard stop at 225 to leave a safety buffer.

The monthly call count is persisted in SQLite (adanos_usage table) so the
budget guard survives process restarts.

Typical usage:
    client = AdanosClient(api_key=settings.adanos_api_key, db_conn=conn)
    tickers = client.get_buzzing_tickers()  # raises BudgetExhausted if limit reached
"""
from datetime import datetime, timezone

import requests
from loguru import logger
from sqlalchemy.engine import Connection

import db.repository as repo

ADANOS_BASE_URL = "https://api.adanossentiment.com"  # update if endpoint changes
BUDGET_LIMIT = 225  # hard stop before the 250/month free-tier cap


class BudgetExhausted(Exception):
    """Raised when the Adanos monthly call budget has been reached.

    The morning cycle catches this exception and skips the day's run
    without error to avoid wasting calls when the budget is depleted.
    """


class AdanosClient:
    """REST client for the Adanos Market Sentiment API.

    Enforces a monthly call budget by tracking usage in the adanos_usage
    SQLite table. Every call to get_buzzing_tickers() checks the count
    before making the HTTP request and increments it afterward.

    Attributes:
        _api_key: Adanos API key for Bearer token authentication.
        _conn: Active SQLAlchemy connection for budget DB reads and writes.
    """

    def __init__(self, api_key: str, db_conn: Connection):
        """Initialize the client with API credentials and a DB connection.

        Args:
            api_key: Adanos API key.
            db_conn: Active SQLAlchemy connection used for budget tracking.
        """
        self._api_key = api_key
        self._conn = db_conn

    def _check_budget(self) -> int:
        """Check the current month's call count against the budget limit.

        Returns:
            Current call count for the month.

        Raises:
            BudgetExhausted: If the count has reached or exceeded BUDGET_LIMIT.
        """
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        count = repo.get_adanos_call_count(self._conn, month)
        if count >= BUDGET_LIMIT:
            raise BudgetExhausted(
                f"Adanos monthly budget exhausted: {count}/{BUDGET_LIMIT} calls used in {month}."
            )
        return count

    def _record_call(self) -> int:
        """Increment the call counter for the current month.

        Returns:
            Updated call count after the increment.
        """
        month = datetime.now(timezone.utc).strftime("%Y-%m")
        return repo.increment_adanos_calls(self._conn, month)

    def get_buzzing_tickers(self) -> list[dict]:
        """Fetch today's buzzing tickers from Adanos and return normalized results.

        Checks the monthly budget before making the HTTP request. Increments
        the call counter only on a successful response.

        Returns:
            List of dicts, each with keys:
                ticker (str): Stock symbol.
                sentiment_score (float): Score in [-1.0, 1.0].
                buzz_rank (int): Rank among all tracked tickers today.
                source_count (int): Number of sources mentioning the ticker.
            Returns an empty list on HTTP or parsing errors.

        Raises:
            BudgetExhausted: If the monthly call limit has been reached.
        """
        self._check_budget()

        headers = {"Authorization": f"Bearer {self._api_key}"}
        try:
            resp = requests.get(
                f"{ADANOS_BASE_URL}/v1/buzz",
                headers=headers,
                timeout=15,
            )
            resp.raise_for_status()
            self._record_call()
            data = resp.json()
            return _parse_adanos_response(data)
        except BudgetExhausted:
            raise
        except Exception as e:
            logger.error(f"Adanos API call failed: {e}")
            return []


def _parse_adanos_response(data: dict | list) -> list[dict]:
    """Parse the Adanos API response into a normalized list of ticker dicts.

    Handles two response shapes observed in the Adanos API:
        - A bare list of ticker objects.
        - A dict with a "data" or "tickers" key containing the list.

    Args:
        data: Parsed JSON response from the Adanos API.

    Returns:
        List of normalized ticker dicts with keys: ticker, sentiment_score,
        buzz_rank, source_count. Tickers with missing symbol fields are skipped.
        Sentiment scores are clamped to [-1.0, 1.0].
    """
    items = data if isinstance(data, list) else data.get("data", data.get("tickers", []))
    results = []
    for item in items:
        ticker = item.get("ticker") or item.get("symbol") or item.get("name")
        if not ticker:
            continue
        score = float(item.get("sentiment_score", item.get("sentiment", 0.0)))
        results.append({
            "ticker": ticker.upper(),
            "sentiment_score": max(-1.0, min(1.0, score)),
            "buzz_rank": item.get("rank", item.get("buzz_rank", 0)),
            "source_count": item.get("source_count", item.get("mentions", 0)),
        })
    return results

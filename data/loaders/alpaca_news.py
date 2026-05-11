"""Alpaca News API client for fetching recent news headlines.

Uses the Alpaca StockHistoricalDataClient to retrieve news articles for a list
of tickers. The API is rate-limited at 200 calls per minute via the rate_limit
decorator.

Typical usage:
    client = AlpacaNewsClient(api_key, secret_key)
    news_by_ticker = client.get_news(["AAPL", "NVDA"], limit=3)
    headline = client.get_top_headline("AAPL")
"""
from datetime import datetime, timedelta, timezone
from typing import Optional

from alpaca.data.historical import StockHistoricalDataClient
from alpaca.data.requests import NewsRequest
from loguru import logger

from utils.rate_limiter import rate_limit


class AlpacaNewsClient:
    """Fetches recent news headlines from the Alpaca News API.

    News articles are fetched in bulk for a list of tickers and then
    distributed per ticker. The rate limiter ensures the client stays
    within Alpaca's 200 calls/minute limit.

    Attributes:
        _client: Alpaca StockHistoricalDataClient used for the news endpoint.
    """

    def __init__(self, api_key: str, secret_key: str):
        """Initialize with Alpaca API credentials.

        Args:
            api_key: Alpaca API key ID.
            secret_key: Alpaca API secret key.
        """
        self._client = StockHistoricalDataClient(api_key, secret_key)

    @rate_limit(calls_per_min=200)
    def _fetch_news(self, tickers: list[str], limit: int, start: datetime) -> list:
        """Make the raw Alpaca news API call.

        Decorated with @rate_limit to stay within 200 calls/minute.

        Args:
            tickers: List of stock symbols to fetch news for.
            limit: Maximum total articles to return across all tickers.
            start: Earliest article publication time to include.

        Returns:
            List of Alpaca news article objects.
        """
        req = NewsRequest(symbols=",".join(tickers), limit=limit, start=start)
        news = self._client.get_news(req)
        return news.data.get("news", []) if hasattr(news, "data") else []

    def get_news(self, tickers: list[str], limit: int = 5, lookback_days: int = 3) -> dict[str, list[dict]]:
        """Fetch recent news for a list of tickers and return results per ticker.

        Args:
            tickers: List of stock symbols.
            limit: Maximum articles to return per ticker.
            lookback_days: How many days back to search for articles.

        Returns:
            Dict mapping each ticker to a list of article dicts, each with keys:
                headline (str): Article title.
                url (str): Full article URL.
                published_at (str): ISO 8601 publication timestamp.
            Tickers with no articles map to an empty list.
        """
        start = datetime.now(timezone.utc) - timedelta(days=lookback_days)
        result: dict[str, list[dict]] = {t: [] for t in tickers}

        try:
            articles = self._fetch_news(tickers, limit * len(tickers), start)
            for article in articles:
                headline = getattr(article, "headline", None) or getattr(article, "title", None) or ""
                url = getattr(article, "url", "") or ""
                published = str(getattr(article, "created_at", "") or "")
                symbols = getattr(article, "symbols", []) or []

                entry = {"headline": headline, "url": url, "published_at": published}
                for sym in symbols:
                    if sym in result and len(result[sym]) < limit:
                        result[sym].append(entry)

        except Exception as e:
            logger.warning(f"Alpaca news fetch failed: {e}")

        return result

    def get_top_headline(self, ticker: str) -> Optional[str]:
        """Return the most recent headline for a single ticker.

        Args:
            ticker: Stock symbol.

        Returns:
            Headline string, or None if no recent news is available.
        """
        news = self.get_news([ticker], limit=1)
        articles = news.get(ticker, [])
        return articles[0]["headline"] if articles else None

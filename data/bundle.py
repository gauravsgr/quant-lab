"""DataBundle: a single pre-fetched snapshot of all market data for one trading day.

The DataBundle is built once per morning cycle and passed unchanged to every
strategy. Expensive fetches (bars, news) are cached to disk per calendar date;
fresh sources (political trades, Adanos buzz) are always fetched live.

Bars are batch-fetched in one Alpaca API call for all watchlist tickers. News is
batch-fetched in groups of 50. Ratings are fetched only for buzz tickers to stay
within yfinance rate limits. Company info (name, description, sector) is fetched
for buzz tickers and any tickers with political/catalyst hits.

Typical usage (inside orchestrator):
    cache = CacheManager()
    bundle = DataBundle.build(broker, settings, cache, adanos_client, news_client,
                              capitol_scraper)
"""
from __future__ import annotations

import io
import contextlib
import re
import yaml
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Optional

from loguru import logger

if TYPE_CHECKING:
    from brokers.base import Broker
    from config.settings import Settings
    from data.cache_manager import CacheManager
    from data.loaders.adanos import AdanosClient
    from data.loaders.alpaca_news import AlpacaNewsClient
    from data.loaders.capitol_trades import CapitolTradesScraper


CATALYST_PATTERNS: dict[str, list[str]] = {
    "supply_deal": [
        "supply agreement", "supply deal", "manufacturing deal",
        "supply contract", "long-term supply", "multi-year supply",
    ],
    "gov_contract": [
        "awarded contract", "government contract", "dod contract",
        "federal contract", "defense contract", "pentagon contract",
    ],
    "partnership": [
        "partnership", "strategic alliance", "joint venture",
        "collaboration agreement", "strategic collaboration",
    ],
    "gov_program": [
        "bead", "chips act", "inflation reduction", "infrastructure investment",
        "iija", "doe grant", "nih grant", "darpa",
    ],
    "regulatory": [
        "fda approval", "fcc approval", "cleared by", "approved for",
        "receives approval", "fda cleared", "510(k)",
    ],
    "guidance_raise": [
        "raises guidance", "raises outlook", "increases forecast",
        "boosts forecast", "raises full-year", "raises annual",
    ],
}


@dataclass
class DataBundle:
    """All market data needed to run every strategy for one trading day.

    Attributes:
        date: YYYY-MM-DD the bundle was built for.
        watchlist: All tickers in scope (S&P 500 + custom additions).
        bars: ticker → list of 60 OHLCV bar dicts (cached parquet).
        news: ticker → list of up to 5 article dicts with headline/url/published_at.
        ratings: ticker → analyst consensus dict from yfinance.
        company_info: ticker → {name, description, sector, website}.
        political_trades: Raw Capitol Trades rows from last 7 days (fresh).
        buzz: Adanos buzz rows (fresh, budget-limited).
        catalyst_hits: ticker → {catalyst_type, keywords_matched, program, summary}.
    """
    date: str
    watchlist: list[str]
    bars: dict[str, list[dict]] = field(default_factory=dict)
    news: dict[str, list[dict]] = field(default_factory=dict)
    ratings: dict[str, dict] = field(default_factory=dict)
    company_info: dict[str, dict] = field(default_factory=dict)
    political_trades: list[dict] = field(default_factory=list)
    buzz: list[dict] = field(default_factory=list)
    catalyst_hits: dict[str, dict] = field(default_factory=dict)

    # ------------------------------------------------------------------
    # Factory
    # ------------------------------------------------------------------

    @classmethod
    def build(
        cls,
        broker: "Broker",
        settings: "Settings",
        cache: "CacheManager",
        adanos: "AdanosClient",
        news_client: "AlpacaNewsClient",
        capitol: "CapitolTradesScraper",
    ) -> "DataBundle":
        """Fetch and assemble all data for today's trading session.

        1. Load watchlist from config/watchlists/sp500.yaml.
        2. Bars: cache hit → load; miss → batch Alpaca fetch, write cache.
        3. News: cache hit → load; miss → batch Alpaca fetch (50/call), write cache.
        4. Political trades: always fresh from Capitol Trades (last 7 days).
        5. Adanos buzz: always fresh (respects monthly budget).
        6. Ratings + company info: cache hit → load; miss → yfinance for buzz tickers.
        7. Catalyst hits: computed in-process from news (no API, free).

        Args:
            broker: Alpaca broker for bar fetches.
            settings: App settings (API keys etc.).
            cache: CacheManager for daily disk caching.
            adanos: Adanos sentiment API client.
            news_client: Alpaca News API client.
            capitol: Capitol Trades scraper.

        Returns:
            Fully populated DataBundle.
        """
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        watchlist = _load_watchlist()
        logger.info(f"DataBundle: {len(watchlist)} tickers in watchlist")

        # --- Bars ---
        bars = cache.load_bars(today)
        if bars is None:
            logger.info("DataBundle: bars cache miss, batch-fetching from Alpaca...")
            bars = _fetch_bars_batch(broker, watchlist)
            cache.save_bars(today, bars)
            logger.info(f"DataBundle: fetched bars for {len(bars)} tickers, cached")
        else:
            logger.info(f"DataBundle: bars cache hit ({len(bars)} tickers)")

        # --- News ---
        news = cache.load_news(today)
        if news is None:
            logger.info("DataBundle: news cache miss, batch-fetching from Alpaca News...")
            news = _fetch_news_batch(news_client, watchlist)
            cache.save_news(today, news)
            logger.info(f"DataBundle: news fetched for {len(news)} tickers, cached")
        else:
            logger.info(f"DataBundle: news cache hit ({len(news)} tickers)")

        # --- Political trades (always fresh, extends to 7 days) ---
        political_trades = []
        try:
            political_trades = capitol.get_recent_trades(days_back=5)
        except Exception as e:
            logger.warning(f"Capitol Trades fetch failed: {e}")

        # --- Adanos buzz (always fresh) ---
        from data.loaders.adanos import BudgetExhausted
        buzz: list[dict] = []
        try:
            buzz = adanos.get_buzzing_tickers()
        except BudgetExhausted as e:
            logger.warning(f"Adanos budget exhausted: {e}")
        except Exception as e:
            logger.warning(f"Adanos fetch failed: {e}")

        # --- Ratings + company info (cached; fetch for buzz + political tickers) ---
        ratings = cache.load_ratings(today) or {}
        company_info = cache.load_company_info(today) or {}

        priority_tickers = _priority_tickers(buzz, political_trades, watchlist)
        missing_ratings = [t for t in priority_tickers if t not in ratings]
        missing_info = [t for t in priority_tickers if t not in company_info]
        if missing_ratings or missing_info:
            logger.info(f"DataBundle: fetching ratings/info for {len(missing_ratings)} tickers...")
            new_ratings, new_info = _fetch_ratings_and_info(list(set(missing_ratings + missing_info)))
            ratings.update(new_ratings)
            company_info.update(new_info)
            cache.save_ratings(today, ratings)
            cache.save_company_info(today, company_info)

        # --- Catalyst hits (in-process, free) ---
        catalyst_hits = cache.load_catalyst(today)
        if catalyst_hits is None:
            gov_programs = _load_gov_programs()
            catalyst_hits = _scan_catalyst_hits(news, gov_programs)
            cache.save_catalyst(today, catalyst_hits)
            logger.info(f"DataBundle: catalyst hits found for {len(catalyst_hits)} tickers")
        else:
            logger.info(f"DataBundle: catalyst cache hit ({len(catalyst_hits)} tickers)")

        return cls(
            date=today,
            watchlist=watchlist,
            bars=bars,
            news=news,
            ratings=ratings,
            company_info=company_info,
            political_trades=political_trades,
            buzz=buzz,
            catalyst_hits=catalyst_hits,
        )


# ------------------------------------------------------------------
# Internal helpers
# ------------------------------------------------------------------

def _load_watchlist(path: str = "config/watchlists/sp500.yaml") -> list[str]:
    """Load the S&P 500 + custom watchlist from YAML."""
    try:
        with open(path) as f:
            cfg = yaml.safe_load(f)
        tickers = list(cfg.get("tickers", []))
        custom = list(cfg.get("custom", []))
        all_tickers = list(dict.fromkeys(tickers + custom))  # deduplicate, preserve order
        return [str(t).upper() for t in all_tickers if t is not None and t is not False and t is not True and str(t).strip()]
    except Exception as e:
        logger.error(f"Failed to load watchlist from {path}: {e}")
        return []


def _fetch_bars_batch(broker: "Broker", watchlist: list[str], limit: int = 60) -> dict[str, list[dict]]:
    """Batch-fetch OHLCV bars for all watchlist tickers in one Alpaca API call."""
    from alpaca.data.historical import StockHistoricalDataClient
    from alpaca.data.requests import StockBarsRequest
    from alpaca.data.timeframe import TimeFrame

    try:
        client = broker._data  # access the underlying Alpaca data client
        req = StockBarsRequest(
            symbol_or_symbols=watchlist,
            timeframe=TimeFrame.Day,
            limit=limit,
        )
        resp = client.get_stock_bars(req)
        result: dict[str, list[dict]] = {}
        for ticker, bar_list in resp.data.items():
            result[ticker] = [
                {
                    "timestamp": str(b.timestamp),
                    "open": float(b.open),
                    "high": float(b.high),
                    "low": float(b.low),
                    "close": float(b.close),
                    "volume": float(b.volume),
                }
                for b in bar_list
            ]
        return result
    except Exception as e:
        logger.error(f"Batch bars fetch failed: {e}")
        return {}


def _fetch_news_batch(
    news_client: "AlpacaNewsClient",
    watchlist: list[str],
    batch_size: int = 50,
    lookback_days: int = 7,
) -> dict[str, list[dict]]:
    """Fetch news for all watchlist tickers in batches of batch_size."""
    result: dict[str, list[dict]] = {t: [] for t in watchlist}
    for i in range(0, len(watchlist), batch_size):
        batch = watchlist[i : i + batch_size]
        try:
            batch_news = news_client.get_news(batch, limit=5, lookback_days=lookback_days)
            for ticker, articles in batch_news.items():
                if ticker in result:
                    result[ticker] = articles
        except Exception as e:
            logger.warning(f"News batch fetch failed for batch {i//batch_size}: {e}")
    return result


def _priority_tickers(
    buzz: list[dict],
    political_trades: list[dict],
    watchlist: list[str],
    max_tickers: int = 150,
) -> list[str]:
    """Return the highest-priority tickers for ratings/company_info fetching.

    Priority: (1) all Adanos buzz tickers, (2) political trade tickers, (3) rest.
    Capped at max_tickers to keep yfinance fetch time reasonable.
    """
    seen: set[str] = set()
    result: list[str] = []

    for item in buzz:
        t = item.get("ticker", "")
        if t and t not in seen:
            seen.add(t)
            result.append(t)

    for trade in political_trades:
        t = trade.get("ticker", "")
        if t and t not in seen:
            seen.add(t)
            result.append(t)

    for t in watchlist:
        if len(result) >= max_tickers:
            break
        if t not in seen:
            seen.add(t)
            result.append(t)

    return result[:max_tickers]


def _fetch_ratings_and_info(tickers: list[str]) -> tuple[dict, dict]:
    """Fetch analyst ratings and company metadata from yfinance for a list of tickers."""
    import yfinance as yf

    ratings: dict[str, dict] = {}
    company_info: dict[str, dict] = {}

    from data.loaders.yfinance_ratings import get_analyst_rating

    for ticker in tickers:
        # Ratings via existing loader
        ratings[ticker] = get_analyst_rating(ticker)

        # Company info from yfinance .info
        try:
            t = yf.Ticker(ticker)
            with contextlib.redirect_stderr(io.StringIO()):
                info = t.info or {}
            company_info[ticker] = {
                "name": info.get("longName") or info.get("shortName") or ticker,
                "description": _truncate(info.get("longBusinessSummary", ""), 200),
                "sector": info.get("sector", ""),
                "website": info.get("website", ""),
            }
        except Exception as e:
            logger.debug(f"yfinance info failed for {ticker}: {e}")
            company_info[ticker] = {"name": ticker, "description": "", "sector": "", "website": ""}

    return ratings, company_info


def _load_gov_programs(path: str = "config/catalyst/government_programs.yaml") -> dict:
    """Load the government programs YAML that maps program names to beneficiary tickers."""
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.debug(f"Gov programs config not found at {path}: {e}")
        return {}


def _scan_catalyst_hits(
    news: dict[str, list[dict]],
    gov_programs: dict,
) -> dict[str, dict]:
    """Scan all news headlines for catalyst keywords and government program matches.

    Two-layer detection:
        Layer 1: keyword pattern matching across all CATALYST_PATTERNS.
        Layer 2: check if ticker appears in any gov_programs beneficiary list.

    Returns:
        Dict mapping ticker → {catalyst_type, keywords_matched, program, summary}.
        Only tickers with at least one hit are included.
    """
    # Build reverse lookup: ticker → list of program names
    ticker_to_programs: dict[str, list[str]] = {}
    for program_name, program_data in gov_programs.items():
        beneficiaries = program_data.get("beneficiaries", [])
        desc = program_data.get("description", program_name)
        for ticker in beneficiaries:
            ticker_to_programs.setdefault(ticker.upper(), []).append(
                f"{program_name}: {desc}"
            )

    catalyst_hits: dict[str, dict] = {}

    for ticker, articles in news.items():
        if not articles:
            continue

        # Layer 1: keyword match
        matched_types: list[str] = []
        matched_keywords: list[str] = []
        most_recent_article: Optional[dict] = None
        days_ago = None

        for article in articles:
            headline = (article.get("headline") or "").lower()
            pub = article.get("published_at", "")
            article_days_ago = _days_ago(pub)

            for catalyst_type, keywords in CATALYST_PATTERNS.items():
                for kw in keywords:
                    if kw in headline:
                        if catalyst_type not in matched_types:
                            matched_types.append(catalyst_type)
                        if kw not in matched_keywords:
                            matched_keywords.append(kw)
                        if most_recent_article is None or (
                            article_days_ago is not None
                            and (days_ago is None or article_days_ago < days_ago)
                        ):
                            most_recent_article = article
                            days_ago = article_days_ago

        # Layer 2: government program match
        programs = ticker_to_programs.get(ticker.upper(), [])

        if matched_types or programs:
            summary_parts = []
            if matched_keywords and days_ago is not None:
                summary_parts.append(
                    f"Matched '{matched_keywords[0]}' in headline {days_ago}d ago"
                )
            for p in programs:
                summary_parts.append(f"{p}")

            catalyst_hits[ticker] = {
                "catalyst_type": matched_types[0] if matched_types else "gov_program",
                "keywords_matched": matched_keywords,
                "programs": programs,
                "summary": "; ".join(summary_parts),
                "days_ago": days_ago,
                "headline": most_recent_article.get("headline") if most_recent_article else None,
            }

    return catalyst_hits


def _days_ago(published_at: str) -> Optional[int]:
    """Return how many whole days ago a published_at ISO string was, or None."""
    if not published_at:
        return None
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        delta = datetime.now(timezone.utc) - dt
        return max(0, delta.days)
    except Exception:
        return None


def _truncate(text: str, max_len: int) -> str:
    """Truncate text to max_len characters, appending '...' if truncated."""
    if not text:
        return ""
    text = re.sub(r"\s+", " ", text).strip()
    return text[:max_len] + "..." if len(text) > max_len else text

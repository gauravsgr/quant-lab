"""Analyst consensus ratings via yfinance.

Fetches analyst recommendation data using yfinance (Yahoo Finance). No API key
is required. Ratings from the last four reporting periods are tallied by
buy/hold/sell category and converted to a consensus label.

Rate-limited at 120 calls/minute to avoid Yahoo Finance throttling. Ticker
data is fetched one at a time; use get_ratings_batch() for multiple tickers.

Typical usage:
    rating = get_analyst_rating("AAPL")
    ratings_map = get_ratings_batch(["AAPL", "NVDA", "MSFT"])
"""
import io
import time
import contextlib
import yfinance as yf
from loguru import logger

from utils.rate_limiter import rate_limit


# Maps raw yfinance grade strings to canonical 5-level rating labels.
ANALYST_GRADE_MAP = {
    "Strong Buy": "Strong Buy",
    "Buy": "Buy",
    "Hold": "Hold",
    "Neutral": "Hold",
    "Underperform": "Sell",
    "Sell": "Sell",
    "Strong Sell": "Strong Sell",
    "Overweight": "Buy",
    "Underweight": "Sell",
    "Equal-Weight": "Hold",
    "Market Perform": "Hold",
    "Outperform": "Buy",
    "Sector Perform": "Hold",
}

# Numeric consensus score per rating (not used in classification, but available for extensions).
CONSENSUS_SCORE = {
    "Strong Buy": 5,
    "Buy": 4,
    "Hold": 3,
    "Sell": 2,
    "Strong Sell": 1,
}


@rate_limit(calls_per_min=120)
def _fetch_ticker_info(ticker: str) -> dict:
    """Fetch yfinance Ticker object data for a single ticker.

    Decorated with @rate_limit to stay within ~120 calls/minute and avoid
    Yahoo Finance's informal rate limiting.

    Args:
        ticker: Stock symbol.

    Returns:
        Dict with keys:
            recommendations: DataFrame of analyst recommendations (may be None).
            info: Dict of yfinance ticker metadata (includes targetMeanPrice).
    """
    t = yf.Ticker(ticker)
    with contextlib.redirect_stderr(io.StringIO()):
        return {
            "recommendations": t.recommendations,
            "info": t.info,
        }


def get_analyst_rating(ticker: str) -> dict:
    """Fetch and compute analyst consensus for a single ticker.

    Tallies buy, hold, and sell recommendations from the last 4 yfinance
    reporting periods. Consensus is determined by the fraction of buy vs.
    sell votes:
        buy_pct >= 0.75  -> Strong Buy
        buy_pct >= 0.60  -> Buy
        sell_pct >= 0.75 -> Strong Sell
        sell_pct >= 0.60 -> Sell
        otherwise        -> Hold

    Args:
        ticker: Stock symbol.

    Returns:
        Dict with keys:
            ticker (str): Stock symbol.
            rating (str): Consensus label ("Strong Buy" through "Strong Sell").
            buy_count (int): Total buy/strong-buy recommendations tallied.
            hold_count (int): Total hold/neutral recommendations tallied.
            sell_count (int): Total sell/strong-sell recommendations tallied.
            price_target (float or None): Mean analyst price target from yfinance info.
        Returns Hold with zero counts on any fetch or parsing error.
    """
    try:
        data = _fetch_ticker_info(ticker)
        recs = data["recommendations"]
        info = data["info"]

        buy_count = hold_count = sell_count = 0
        if recs is not None and not recs.empty:
            latest = recs.tail(4)  # last 4 reporting periods
            for col in latest.columns:
                col_lower = col.lower()
                if "strong_buy" in col_lower or "strongbuy" in col_lower:
                    buy_count += int(latest[col].sum())
                elif "buy" in col_lower:
                    buy_count += int(latest[col].sum())
                elif "hold" in col_lower or "neutral" in col_lower:
                    hold_count += int(latest[col].sum())
                elif "sell" in col_lower or "underperform" in col_lower:
                    sell_count += int(latest[col].sum())

        total = buy_count + hold_count + sell_count
        if total == 0:
            consensus = "Hold"
        else:
            buy_pct = buy_count / total
            sell_pct = sell_count / total
            if buy_pct >= 0.60:
                consensus = "Strong Buy" if buy_pct >= 0.75 else "Buy"
            elif sell_pct >= 0.60:
                consensus = "Strong Sell" if sell_pct >= 0.75 else "Sell"
            else:
                consensus = "Hold"

        price_target = info.get("targetMeanPrice")

        return {
            "ticker": ticker,
            "rating": consensus,
            "buy_count": buy_count,
            "hold_count": hold_count,
            "sell_count": sell_count,
            "price_target": price_target,
        }
    except Exception as e:
        logger.warning(f"yfinance ratings failed for {ticker}: {e}")
        return {
            "ticker": ticker,
            "rating": "Hold",
            "buy_count": 0,
            "hold_count": 0,
            "sell_count": 0,
            "price_target": None,
        }


def get_ratings_batch(tickers: list[str]) -> dict[str, dict]:
    """Fetch analyst ratings for multiple tickers sequentially.

    Each call to get_analyst_rating() is individually rate-limited, so
    this function does not need additional throttling.

    Args:
        tickers: List of stock symbols.

    Returns:
        Dict mapping each ticker symbol to its analyst rating dict.
    """
    results = {}
    for ticker in tickers:
        results[ticker] = get_analyst_rating(ticker)
    return results

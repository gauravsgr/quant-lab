"""Standalone political trades + news confirmation strategy.

Fires when:
  1. A politician BUY disclosure is present in the Capitol Trades data, AND
  2. At least one relevant news headline exists for that ticker in the last 7 days.

Both conditions are required. News confirmation prevents acting on stale
45-day-lagged disclosures where the thesis has already played out.

Confidence formula:
    seniority_score = 1.0 (Senate), 0.75 (House), 0.5 (Unknown)
    recency_score   = 1.0 (0-3 days), 0.5 (4-7 days)
    confidence      = 0.60 * seniority_score + 0.40 * recency_score

Signal: always STRONG_BUY for BUY disclosures (no short-selling on political data).
Threshold: 0.45
"""
from datetime import datetime, timedelta, timezone
from loguru import logger

from strategies.base import Signal, StandaloneStrategy


THRESHOLD = 0.45
NEWS_LOOKBACK_DAYS = 7


class PoliticalNewsStrategy(StandaloneStrategy):
    """Capitol Trades + Alpaca News dual-confirmation strategy."""

    @property
    def name(self) -> str:
        return "political_news"

    def run(self, bundle) -> list[Signal]:
        if not bundle.political_trades:
            logger.info("PoliticalNewsStrategy: no political trades today, skipping")
            return []

        # Keep only BUY disclosures per ticker (latest per ticker)
        pol_map: dict[str, dict] = {}
        for trade in bundle.political_trades:
            ticker = trade.get("ticker", "")
            action = (trade.get("action") or "").lower()
            if ticker and action in ("buy", "purchase"):
                if ticker not in pol_map:
                    pol_map[ticker] = trade

        if not pol_map:
            logger.info("PoliticalNewsStrategy: no BUY disclosures found")
            return []

        signals: list[Signal] = []

        for ticker, trade in pol_map.items():
            # Require news confirmation
            news_list = bundle.news.get(ticker, [])
            if not news_list:
                logger.debug(f"PoliticalNewsStrategy: no news for {ticker}, skipping")
                continue

            # Pick most recent article
            best_article = news_list[0]
            days_ago = _days_since(best_article.get("published_at", ""))
            if days_ago is None or days_ago > NEWS_LOOKBACK_DAYS:
                logger.debug(f"PoliticalNewsStrategy: news for {ticker} is stale ({days_ago}d), skipping")
                continue

            chamber = (trade.get("chamber") or "Unknown")
            seniority_score = 1.0 if chamber == "Senate" else (0.75 if chamber == "House" else 0.5)
            recency_score = 1.0 if days_ago <= 3 else 0.5

            confidence = round(0.60 * seniority_score + 0.40 * recency_score, 4)
            if confidence < THRESHOLD:
                continue

            rating_data = bundle.ratings.get(ticker, {})
            info = bundle.company_info.get(ticker, {})

            signals.append(Signal(
                ticker=ticker,
                signal_type="STRONG_BUY",
                confidence=confidence,
                order_type="call_option",
                politician_action=trade.get("action"),
                politician_name=trade.get("politician"),
                politician_party=trade.get("party"),
                politician_chamber=chamber,
                politician_amount=trade.get("amount_range"),
                disclosure_url=trade.get("disclosure_url"),
                analyst_rating=rating_data.get("rating", "Hold"),
                analyst_buy_count=rating_data.get("buy_count", 0),
                analyst_hold_count=rating_data.get("hold_count", 0),
                analyst_sell_count=rating_data.get("sell_count", 0),
                analyst_price_target=rating_data.get("price_target"),
                news_headline=best_article.get("headline"),
                news_url=best_article.get("url"),
                strategy_name=self.name,
                company_name=info.get("name", ticker),
                company_description=info.get("description", ""),
                components={
                    "seniority": seniority_score,
                    "news_recency": recency_score,
                    "days_ago": days_ago,
                },
            ))

        signals.sort(key=lambda s: s.confidence, reverse=True)
        logger.info(
            f"PoliticalNewsStrategy: {len(signals)} signals from {len(pol_map)} BUY disclosures"
        )
        return signals


def _days_since(published_at: str) -> int | None:
    """Return whole days since an ISO 8601 publication timestamp."""
    if not published_at:
        return None
    try:
        dt = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
        return max(0, (datetime.now(timezone.utc) - dt).days)
    except Exception:
        return None

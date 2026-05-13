"""Standalone Adanos social sentiment strategy (Reddit · StockTwits).

Generates signals purely from Adanos buzz and sentiment scores. Only fires for
tickers that appear in today's Adanos buzz list. Analyst rating provides a
secondary confidence component.

Confidence formula:
    confidence = 0.60 * abs(sentiment_score) + 0.40 * analyst_score

Signal rules:
    STRONG_BUY:  sentiment_score > 0.02  (net positive social sentiment)
    STRONG_PUT:  sentiment_score < -0.02 (net negative social sentiment)

Threshold: 0.45
"""
from loguru import logger

from strategies.base import Signal, StandaloneStrategy


ANALYST_SCORE_MAP = {
    "Strong Buy": 1.0,
    "Buy": 0.75,
    "Hold": 0.50,
    "Sell": 0.25,
    "Strong Sell": 0.0,
}

THRESHOLD = 0.45
SENTIMENT_MIN = 0.02  # minimum absolute sentiment to generate a signal


class SentimentStrategy(StandaloneStrategy):
    """Adanos buzz-based signal generator — only fires for Reddit-buzzing tickers."""

    @property
    def name(self) -> str:
        return "sentiment"

    def run(self, bundle) -> list[Signal]:
        if not bundle.buzz:
            logger.info("SentimentStrategy: no Adanos buzz data, skipping")
            return []

        signals: list[Signal] = []

        for item in bundle.buzz:
            ticker = item.get("ticker", "")
            sentiment = item.get("sentiment_score", 0.0) or 0.0

            if abs(sentiment) < SENTIMENT_MIN:
                continue

            rating_data = bundle.ratings.get(ticker, {})
            rating = rating_data.get("rating", "Hold")

            if sentiment > 0:
                signal_type = "STRONG_BUY"
                order_type = "call_option"
                analyst_c = ANALYST_SCORE_MAP.get(rating, 0.5)
                sentiment_c = min(1.0, sentiment)
            else:
                signal_type = "STRONG_PUT"
                order_type = "put_option"
                analyst_c = 1.0 - ANALYST_SCORE_MAP.get(rating, 0.5)
                sentiment_c = min(1.0, -sentiment)

            confidence = round(0.60 * sentiment_c + 0.40 * analyst_c, 4)
            if confidence < THRESHOLD:
                continue

            news_list = bundle.news.get(ticker, [])
            headline = news_list[0].get("headline") if news_list else None
            news_url = news_list[0].get("url") if news_list else None
            info = bundle.company_info.get(ticker, {})

            signals.append(Signal(
                ticker=ticker,
                signal_type=signal_type,
                confidence=confidence,
                order_type=order_type,
                sentiment_score=sentiment,
                analyst_rating=rating,
                analyst_buy_count=rating_data.get("buy_count", 0),
                analyst_hold_count=rating_data.get("hold_count", 0),
                analyst_sell_count=rating_data.get("sell_count", 0),
                analyst_price_target=rating_data.get("price_target"),
                news_headline=headline,
                news_url=news_url,
                strategy_name=self.name,
                company_name=info.get("name", ticker),
                company_description=info.get("description", ""),
                components={
                    "sentiment": sentiment_c,
                    "analyst": analyst_c,
                    "buzz_score": item.get("buzz_rank", 0),
                    "source_count": item.get("source_count", 0),
                },
            ))

        signals.sort(key=lambda s: s.confidence, reverse=True)
        logger.info(f"SentimentStrategy: {len(signals)} signals from {len(bundle.buzz)} buzz tickers")
        return signals

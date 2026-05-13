"""Standalone technical analysis strategy (RSI · MACD · Moving Averages).

Scans every ticker in the DataBundle for which bars are available and applies
the same composite technical indicator scoring used by TechnicalBuzzEnsemble.
No Adanos sentiment or political data is required — purely chart-based.

Confidence formula:
    confidence = 0.70 * abs(tech_score) + 0.30 * analyst_score
    where analyst_score = ANALYST_SCORE_MAP[rating] for BUY,
          1 - ANALYST_SCORE_MAP[rating] for PUT.

Signal threshold: 0.45 (lower than ensemble — standalone is one input, not final)
"""
from loguru import logger

from data.loaders.technical import compute_technical_signal
from strategies.base import Signal, StandaloneStrategy


ANALYST_SCORE_MAP = {
    "Strong Buy": 1.0,
    "Buy": 0.75,
    "Hold": 0.50,
    "Sell": 0.25,
    "Strong Sell": 0.0,
}

THRESHOLD = 0.45


class TechnicalStrategy(StandaloneStrategy):
    """RSI/MACD/MA strategy that runs on all tickers with available bars."""

    @property
    def name(self) -> str:
        return "technical"

    def run(self, bundle) -> list[Signal]:
        signals: list[Signal] = []

        for ticker, bars in bundle.bars.items():
            try:
                tech = compute_technical_signal(bars)
            except Exception as e:
                logger.debug(f"TechnicalStrategy: bars error for {ticker}: {e}")
                continue

            tech_score = tech.get("score", 0.0) or 0.0
            tech_direction = tech.get("direction", "neutral")

            if tech_direction == "neutral" or abs(tech_score) < 0.10:
                continue

            rating_data = bundle.ratings.get(ticker, {})
            rating = rating_data.get("rating", "Hold")

            if tech_direction == "bullish":
                signal_type = "STRONG_BUY"
                analyst_c = ANALYST_SCORE_MAP.get(rating, 0.5)
                order_type = "call_option"
                tech_c = max(0.0, tech_score)
            else:
                signal_type = "STRONG_PUT"
                analyst_c = 1.0 - ANALYST_SCORE_MAP.get(rating, 0.5)
                order_type = "put_option"
                tech_c = max(0.0, -tech_score)

            confidence = round(0.70 * tech_c + 0.30 * analyst_c, 4)
            if confidence < THRESHOLD:
                continue

            info = bundle.company_info.get(ticker, {})
            news_list = bundle.news.get(ticker, [])
            signals.append(Signal(
                ticker=ticker,
                signal_type=signal_type,
                confidence=confidence,
                order_type=order_type,
                analyst_rating=rating,
                analyst_buy_count=rating_data.get("buy_count", 0),
                analyst_hold_count=rating_data.get("hold_count", 0),
                analyst_sell_count=rating_data.get("sell_count", 0),
                analyst_price_target=rating_data.get("price_target"),
                news_headline=news_list[0].get("headline") if news_list else None,
                news_url=news_list[0].get("url") if news_list else None,
                technical_score=tech_score,
                technical_direction=tech_direction,
                technical_rsi=tech.get("rsi"),
                strategy_name=self.name,
                company_name=info.get("name", ticker),
                company_description=info.get("description", ""),
                components={
                    "tech_score": tech_score,
                    "analyst": analyst_c,
                    "macd": tech.get("macd", "neutral"),
                    "ma": tech.get("ma", "neutral"),
                },
            ))

        signals.sort(key=lambda s: s.confidence, reverse=True)
        logger.info(f"TechnicalStrategy: {len(signals)} signals from {len(bundle.bars)} tickers")
        return signals

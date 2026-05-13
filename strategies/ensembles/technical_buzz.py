"""TechnicalBuzzEnsemble: technical indicators + Adanos sentiment combined.

Migrated from the previous monolithic TechnicalBuzzStrategy (strategies/technical_buzz.py)
to the new StandaloneStrategy interface. The logic is unchanged — only the input
source changed from individual orchestrator calls to a DataBundle.

Confidence formula:
    technical_c  = max(0, tech_score)     for STRONG_BUY
                 = max(0, -tech_score)    for STRONG_PUT
    sentiment_c  = max(0, sentiment)      for STRONG_BUY
                 = max(0, -sentiment)     for STRONG_PUT
    analyst_c    = ANALYST_SCORE_MAP[rating] for STRONG_BUY
                 = 1 - ANALYST_SCORE_MAP[rating] for STRONG_PUT

    confidence = 0.50 * technical_c + 0.25 * sentiment_c + 0.25 * analyst_c
               + politician_boost (0.10 if pol direction aligns, capped at 1.0)

Only tickers in the Adanos buzz list are considered. Technical direction and
sentiment must agree (bullish + ≥0 sentiment, or bearish + ≤0 sentiment).

Threshold: 0.55 (higher than standalone — this is an ensemble of two signals)
"""
import yaml
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

THRESHOLD = 0.55
POLITICIAN_BOOST = 0.10


class TechnicalBuzzEnsemble(StandaloneStrategy):
    """Technical + Adanos sentiment ensemble (Adanos tickers only)."""

    @property
    def name(self) -> str:
        return "technical_buzz_ensemble"

    def run(self, bundle) -> list[Signal]:
        if not bundle.buzz:
            logger.info("TechnicalBuzzEnsemble: no Adanos buzz data, skipping")
            return []

        # Build buzz lookup
        buzz_map = {
            item["ticker"]: item
            for item in bundle.buzz
            if item.get("ticker")
        }

        # Sort by buzz_score desc, take top 30
        top_tickers = sorted(
            buzz_map.keys(),
            key=lambda t: buzz_map[t].get("buzz_rank", 0),
            reverse=True,
        )[:30]

        # Build political trade lookup
        pol_map: dict[str, dict] = {}
        for trade in bundle.political_trades:
            t = trade.get("ticker", "")
            if t and t not in pol_map:
                pol_map[t] = trade

        signals: list[Signal] = []

        for ticker in top_tickers:
            bars = bundle.bars.get(ticker)
            if not bars:
                continue

            try:
                tech = compute_technical_signal(bars)
            except Exception as e:
                logger.debug(f"TechnicalBuzzEnsemble: technical error for {ticker}: {e}")
                continue

            tech_score = tech.get("score", 0.0) or 0.0
            tech_direction = tech.get("direction", "neutral")
            buzz_item = buzz_map[ticker]
            sentiment = buzz_item.get("sentiment_score", 0.0) or 0.0

            if tech_direction == "bullish" and sentiment >= 0:
                signal_type = "STRONG_BUY"
                order_type = "call_option"
                technical_c = max(0.0, tech_score)
                sentiment_c = max(0.0, sentiment)
                pol_aligned = (pol_map.get(ticker, {}).get("action", "").lower() in ("buy", "purchase"))
            elif tech_direction == "bearish" and sentiment <= 0:
                signal_type = "STRONG_PUT"
                order_type = "put_option"
                technical_c = max(0.0, -tech_score)
                sentiment_c = max(0.0, -sentiment)
                pol_aligned = (pol_map.get(ticker, {}).get("action", "").lower() in ("sell", "sale"))
            else:
                continue  # direction mismatch

            rating_data = bundle.ratings.get(ticker, {})
            rating = rating_data.get("rating", "Hold")
            if signal_type == "STRONG_BUY":
                analyst_c = ANALYST_SCORE_MAP.get(rating, 0.5)
            else:
                analyst_c = 1.0 - ANALYST_SCORE_MAP.get(rating, 0.5)

            confidence = 0.50 * technical_c + 0.25 * sentiment_c + 0.25 * analyst_c
            if pol_aligned:
                confidence += POLITICIAN_BOOST
            confidence = round(min(1.0, confidence), 4)

            if confidence < THRESHOLD:
                continue

            pol = pol_map.get(ticker, {})
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
                politician_action=pol.get("action"),
                politician_name=pol.get("politician"),
                politician_party=pol.get("party"),
                politician_chamber=pol.get("chamber"),
                politician_amount=pol.get("amount_range"),
                disclosure_url=pol.get("disclosure_url"),
                analyst_rating=rating,
                analyst_buy_count=rating_data.get("buy_count", 0),
                analyst_hold_count=rating_data.get("hold_count", 0),
                analyst_sell_count=rating_data.get("sell_count", 0),
                analyst_price_target=rating_data.get("price_target"),
                news_headline=headline,
                news_url=news_url,
                technical_score=tech_score,
                technical_direction=tech_direction,
                technical_rsi=tech.get("rsi"),
                strategy_name=self.name,
                company_name=info.get("name", ticker),
                company_description=info.get("description", ""),
                components={
                    "technical": technical_c,
                    "sentiment": sentiment_c,
                    "analyst": analyst_c,
                    "macd": tech.get("macd", "neutral"),
                    "ma": tech.get("ma", "neutral"),
                },
            ))

        signals.sort(key=lambda s: s.confidence, reverse=True)
        logger.info(f"TechnicalBuzzEnsemble: {len(signals)} signals from {len(top_tickers)} buzz tickers")
        return signals

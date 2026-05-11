"""Technical-momentum strategy for the alternative-data agent.

Combines technical indicators (RSI, MACD, moving averages) with Adanos social
sentiment and analyst consensus to generate buy/put signals. Technical analysis
determines the primary direction; sentiment must agree; analyst rating adds
conviction. Congressional disclosures provide an optional confidence boost.

Confidence formula:
    technical_c  = max(0, technical_score)     for STRONG_BUY
                 = max(0, -technical_score)    for STRONG_PUT   (range [0, 1])
    sentiment_c  = max(0, sentiment_score)     for STRONG_BUY
                 = max(0, -sentiment_score)    for STRONG_PUT   (range [0, 1])
    analyst_c    = analyst_score_map[rating]   for STRONG_BUY
                 = 1 - analyst_score_map[rating] for STRONG_PUT  (range [0, 1])

    confidence = w_t * technical_c + w_s * sentiment_c + w_a * analyst_c
               + politician_boost  (if pol_action aligns, capped at 1.0)

Signal classification:
    STRONG_BUY:  technical direction == "bullish" AND sentiment_score >= 0
    STRONG_PUT:  technical direction == "bearish" AND sentiment_score <= 0
    NEUTRAL:     technicals and sentiment disagree, or direction == "neutral"
"""
from dataclasses import dataclass
from typing import Optional

import yaml

from strategies.base import Signal, SignalType, OrderType


ANALYST_SCORE_MAP = {
    "Strong Buy": 1.0,
    "Buy": 0.75,
    "Hold": 0.50,
    "Sell": 0.25,
    "Strong Sell": 0.0,
}


@dataclass(frozen=True)
class TechnicalBuzzConfig:
    """Immutable hyperparameter snapshot loaded from technical_buzz.yaml."""
    technical_weight: float
    sentiment_weight: float
    analyst_weight: float
    min_confidence: float
    buzz_top_n: int
    politician_boost: float


def load_technical_buzz_config(path: str = "config/strategies/technical_buzz.yaml") -> TechnicalBuzzConfig:
    with open(path) as f:
        cfg = yaml.safe_load(f)
    return TechnicalBuzzConfig(
        technical_weight=cfg["weights"]["technical"],
        sentiment_weight=cfg["weights"]["sentiment"],
        analyst_weight=cfg["weights"]["analyst"],
        min_confidence=cfg["thresholds"]["min_confidence_to_trade"],
        buzz_top_n=cfg["thresholds"]["buzz_top_n"],
        politician_boost=cfg.get("politician_boost", 0.10),
    )


class TechnicalBuzzStrategy:
    """Signal generator using technical indicators as primary direction signal.

    Adanos buzz score acts as a screener; tickers are pre-filtered by the
    orchestrator before reaching this strategy. For each filtered ticker,
    the orchestrator computes a technical signal dict (from
    data/loaders/technical.py) and passes it in alongside the alternative-data
    inputs already used by ConfluenceStrategy.
    """

    def __init__(self, config: Optional[TechnicalBuzzConfig] = None):
        self.cfg = config or load_technical_buzz_config()

    def generate_signal(
        self,
        ticker: str,
        technical: Optional[dict] = None,
        sentiment_score: Optional[float] = None,
        politician_action: Optional[str] = None,
        politician_name: Optional[str] = None,
        politician_party: Optional[str] = None,
        politician_chamber: Optional[str] = None,
        politician_amount: Optional[str] = None,
        disclosure_url: Optional[str] = None,
        analyst_rating: Optional[str] = None,
        analyst_buy_count: int = 0,
        analyst_hold_count: int = 0,
        analyst_sell_count: int = 0,
        analyst_price_target: Optional[float] = None,
        news_headline: Optional[str] = None,
    ) -> Signal:
        """Generate a signal for a single ticker.

        Args:
            ticker: Stock symbol.
            technical: Dict from compute_technical_signal() with keys:
                score (float [-1,1]), direction (str), rsi (float), macd (str), ma (str).
                Defaults to neutral if None or empty.
            sentiment_score: Adanos sentiment in [-1.0, 1.0].
            politician_action: "BUY" or "SELL" from STOCK Act disclosure.
            analyst_rating: Consensus string from yfinance.
            (remaining args): metadata carried through to Signal for display/audit.

        Returns:
            Signal with type, confidence, technical fields populated.
        """
        tech = technical or {}
        tech_score = tech.get("score", 0.0) or 0.0
        tech_direction = tech.get("direction", "neutral") or "neutral"
        sentiment = sentiment_score or 0.0
        pa = (politician_action or "").upper()

        signal_type, order_type = self._classify(tech_direction, sentiment)

        if signal_type == "STRONG_BUY":
            technical_c = max(0.0, tech_score)
            sentiment_c = max(0.0, sentiment)
            analyst_c = ANALYST_SCORE_MAP.get(analyst_rating or "Hold", 0.5)
            pol_aligned = pa in ("BUY", "PURCHASE")
        elif signal_type == "STRONG_PUT":
            technical_c = max(0.0, -tech_score)
            sentiment_c = max(0.0, -sentiment)
            analyst_c = 1.0 - ANALYST_SCORE_MAP.get(analyst_rating or "Hold", 0.5)
            pol_aligned = pa in ("SELL", "SALE")
        else:
            technical_c = abs(tech_score)
            sentiment_c = abs(sentiment)
            analyst_c = abs(ANALYST_SCORE_MAP.get(analyst_rating or "Hold", 0.5) - 0.5) * 2
            pol_aligned = False

        confidence = (
            self.cfg.technical_weight * technical_c
            + self.cfg.sentiment_weight * sentiment_c
            + self.cfg.analyst_weight * analyst_c
        )
        if pol_aligned:
            confidence += self.cfg.politician_boost
        confidence = round(min(1.0, max(0.0, confidence)), 4)

        return Signal(
            ticker=ticker,
            signal_type=signal_type,
            confidence=confidence,
            order_type=order_type,
            sentiment_score=sentiment_score,
            politician_action=politician_action,
            politician_name=politician_name,
            politician_party=politician_party,
            politician_chamber=politician_chamber,
            politician_amount=politician_amount,
            disclosure_url=disclosure_url,
            analyst_rating=analyst_rating,
            analyst_buy_count=analyst_buy_count,
            analyst_hold_count=analyst_hold_count,
            analyst_sell_count=analyst_sell_count,
            analyst_price_target=analyst_price_target,
            news_headline=news_headline,
            technical_score=tech_score,
            technical_direction=tech_direction,
            technical_rsi=tech.get("rsi"),
            components={
                "technical": technical_c,
                "sentiment": sentiment_c,
                "analyst": analyst_c,
                "macd": tech.get("macd", "neutral"),
                "ma": tech.get("ma", "neutral"),
            },
        )

    def _classify(
        self,
        tech_direction: str,
        sentiment: float,
    ) -> tuple[SignalType, Optional[OrderType]]:
        """Determine signal type from technical direction and sentiment alignment.

        Both technical and sentiment must point in the same direction to produce
        an actionable signal. Mixed or neutral readings produce NEUTRAL.
        """
        if tech_direction == "bullish" and sentiment >= 0:
            return "STRONG_BUY", "call_option"
        if tech_direction == "bearish" and sentiment <= 0:
            return "STRONG_PUT", "put_option"
        return "NEUTRAL", None

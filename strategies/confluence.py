"""Confluence signal generator for the alternative-data trading agent.

Combines three independent alternative data sources into a weighted confidence
score and classifies each ticker as STRONG_BUY, STRONG_PUT, or NEUTRAL.

Design note: ConfluenceStrategy.generate_signal() takes structured keyword
arguments rather than an OHLCV DataFrame. The base Strategy contract is
designed for price-bar strategies; this strategy consumes heterogeneous
pre-fetched signals (a sentiment float, a political categorical, an analyst
categorical). Forcing a DataFrame wrapper would add indirection with no
benefit. Both are logically strategies but serve different input contracts.

All weights and thresholds are read from config/strategies/confluence.yaml
and stored in the frozen ConfluenceConfig dataclass.
"""
from dataclasses import dataclass
from typing import Optional

import yaml

from strategies.base import Signal, SignalType, OrderType


# Maps analyst rating strings to a [0, 1] bullishness score.
ANALYST_SCORE_MAP = {
    "Strong Buy": 1.0,
    "Buy": 0.75,
    "Hold": 0.5,
    "Sell": 0.25,
    "Strong Sell": 0.0,
}


@dataclass(frozen=True)
class ConfluenceConfig:
    """Immutable hyperparameter snapshot loaded from confluence.yaml.

    Attributes:
        sentiment_weight: Weight for the sentiment component in [0, 1].
        politician_weight: Weight for the politician component in [0, 1].
        analyst_weight: Weight for the analyst component in [0, 1].
        min_confidence: Minimum confidence score required to submit an order.
        strong_buy_threshold: Minimum sentiment score for a bullish classification.
        strong_put_threshold: Maximum (most negative) sentiment score for a bearish one.
    """
    sentiment_weight: float
    politician_weight: float
    analyst_weight: float
    min_confidence: float
    strong_buy_threshold: float
    strong_put_threshold: float


def load_confluence_config(path: str = "config/strategies/confluence.yaml") -> ConfluenceConfig:
    """Read confluence.yaml and return a ConfluenceConfig.

    Args:
        path: Path to the YAML config file.

    Returns:
        A frozen ConfluenceConfig dataclass.

    Raises:
        FileNotFoundError: If the YAML file does not exist at `path`.
        KeyError: If expected YAML keys are missing.
    """
    with open(path) as f:
        cfg = yaml.safe_load(f)
    return ConfluenceConfig(
        sentiment_weight=cfg["weights"]["sentiment"],
        politician_weight=cfg["weights"]["politician"],
        analyst_weight=cfg["weights"]["analyst"],
        min_confidence=cfg["thresholds"]["min_confidence_to_trade"],
        strong_buy_threshold=cfg["thresholds"]["strong_buy_sentiment"],
        strong_put_threshold=cfg["thresholds"]["strong_put_sentiment"],
    )


class ConfluenceStrategy:
    """Signal generator that scores tickers using three alternative data sources.

    Each source contributes a component score in [0, 1] representing bullishness.
    The final confidence is a weighted sum of the three components. Signal
    classification applies additional threshold rules on top of the confidence score.

    Note: Unlike OHLCV strategies, generate_signal() takes structured keyword
    arguments. See module docstring for the rationale.
    """

    def __init__(self, config: Optional[ConfluenceConfig] = None):
        """Initialize with explicit config or load from the default YAML path.

        Args:
            config: A pre-built ConfluenceConfig; if None, loads from the
                default config/strategies/confluence.yaml.
        """
        self.cfg = config or load_confluence_config()

    def generate_signal(
        self,
        ticker: str,
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
        """Compute a confidence-weighted signal for a single ticker.

        Confidence formula:
            confidence = w_s * sentiment_c + w_p * politician_c + w_a * analyst_c

        Signal classification rules:
            STRONG_BUY: sentiment > 0.7 AND politician == BUY AND analyst == Strong Buy
            STRONG_PUT: sentiment < -0.7 AND (politician == SELL OR analyst in Sell/Strong Sell)
            NEUTRAL: everything else

        Missing inputs default to a neutral (0.5) component score, which
        prevents strong signals when data is unavailable.

        Args:
            ticker: Stock symbol.
            sentiment_score: Adanos sentiment in [-1.0, 1.0]; None uses 0.5.
            politician_action: "BUY" or "SELL" from STOCK Act; None uses 0.5.
            politician_name: Politician display name (for audit trail only).
            politician_party: Party abbreviation (for audit trail only).
            politician_chamber: "Senate" or "House" (for audit trail only).
            politician_amount: Dollar range string (for audit trail only).
            disclosure_url: URL to the Capitol Trades disclosure page.
            analyst_rating: Consensus string; None uses "Hold" (0.5).
            analyst_buy_count: Buy recommendation count.
            analyst_hold_count: Hold recommendation count.
            analyst_sell_count: Sell recommendation count.
            analyst_price_target: Mean analyst price target in USD.
            news_headline: Most recent headline (for Slack display only).

        Returns:
            A Signal instance with type, confidence, and all input metadata.
        """
        sentiment_c = self._sentiment_component(sentiment_score)
        politician_c = self._politician_component(politician_action)
        analyst_c = self._analyst_component(analyst_rating)

        confidence = (
            self.cfg.sentiment_weight * sentiment_c
            + self.cfg.politician_weight * politician_c
            + self.cfg.analyst_weight * analyst_c
        )
        confidence = round(min(1.0, max(0.0, confidence)), 4)

        signal_type, order_type = self._classify(
            sentiment_score, politician_action, analyst_rating, confidence
        )

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
            components={
                "sentiment": sentiment_c,
                "politician": politician_c,
                "analyst": analyst_c,
            },
        )

    def _sentiment_component(self, score: Optional[float]) -> float:
        """Convert a sentiment score to a bullishness component in [0, 1].

        Positive scores map linearly to [0, 1]; negative scores are clamped to 0.
        The classifier handles the bearish case separately via threshold rules.

        Args:
            score: Sentiment score in [-1.0, 1.0], or None for missing data.

        Returns:
            A float in [0.0, 1.0].
        """
        if score is None:
            return 0.5
        return max(0.0, min(1.0, score))

    def _politician_component(self, action: Optional[str]) -> float:
        """Convert a politician action string to a bullishness component.

        Args:
            action: "BUY" or "SELL" (case-insensitive), or None for no disclosure.

        Returns:
            1.0 for buy, 0.0 for sell, 0.5 for missing or unknown action.
        """
        if action is None:
            return 0.5
        action = action.upper()
        if action in ("BUY", "PURCHASE"):
            return 1.0
        if action in ("SELL", "SALE"):
            return 0.0
        return 0.5

    def _analyst_component(self, rating: Optional[str]) -> float:
        """Convert an analyst rating string to a bullishness component.

        Args:
            rating: Consensus string from ANALYST_SCORE_MAP, or None for missing data.

        Returns:
            A float in [0.0, 1.0]; defaults to 0.5 (Hold) for unknown ratings.
        """
        return ANALYST_SCORE_MAP.get(rating or "Hold", 0.5)

    def _classify(
        self,
        sentiment: Optional[float],
        politician_action: Optional[str],
        analyst_rating: Optional[str],
        confidence: float,
    ) -> tuple[SignalType, Optional[OrderType]]:
        """Apply threshold rules to determine signal type and order type.

        Args:
            sentiment: Raw sentiment score or None.
            politician_action: Raw action string or None.
            analyst_rating: Raw rating string or None.
            confidence: Pre-computed weighted confidence score.

        Returns:
            Tuple of (signal_type, order_type). order_type is None for NEUTRAL.
        """
        s = sentiment or 0.0
        pa = (politician_action or "").upper()
        ar = analyst_rating or "Hold"

        is_bullish_sentiment = s >= self.cfg.strong_buy_threshold
        is_bearish_sentiment = s <= self.cfg.strong_put_threshold
        is_politician_buy = pa in ("BUY", "PURCHASE")
        is_politician_sell = pa in ("SELL", "SALE")
        is_strong_buy_analyst = ar == "Strong Buy"
        is_bearish_analyst = ar in ("Sell", "Strong Sell")

        if is_bullish_sentiment and is_politician_buy and is_strong_buy_analyst:
            return "STRONG_BUY", "call_option"

        if is_bearish_sentiment and (is_politician_sell or is_bearish_analyst):
            return "STRONG_PUT", "put_option"

        return "NEUTRAL", None

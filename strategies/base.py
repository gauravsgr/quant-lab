"""Abstract base classes and shared types for all trading strategies.

Every concrete strategy must subclass Strategy and implement generate_signal().
The Signal dataclass is the single output type used throughout the execution
pipeline, from strategy generation through broker submission and DB storage.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass, field
from typing import Literal, Optional

import pandas as pd


SignalType = Literal["STRONG_BUY", "STRONG_PUT", "NEUTRAL"]
OrderType = Literal["call_option", "put_option", "equity_long", "equity_short"]


@dataclass
class Signal:
    """Fully-specified trading signal produced by a strategy.

    Carries all inputs that informed the decision (for audit and Slack display)
    plus the derived confidence score and recommended order type.

    Attributes:
        ticker: Stock symbol (e.g., "AAPL").
        signal_type: STRONG_BUY, STRONG_PUT, or NEUTRAL.
        confidence: Weighted composite score in [0.0, 1.0].
        order_type: Execution type; None for NEUTRAL signals.
        sentiment_score: Adanos sentiment in [-1.0, 1.0]; None if not available.
        politician_action: "BUY" or "SELL" from STOCK Act disclosure; None if none.
        politician_name: Name of the disclosing politician.
        politician_party: Party abbreviation ("R", "D", "I").
        politician_chamber: "Senate" or "House".
        politician_amount: Dollar range string from the disclosure.
        disclosure_url: Direct URL to the Capitol Trades disclosure page.
        analyst_rating: Consensus string ("Strong Buy", "Buy", "Hold", "Sell", "Strong Sell").
        analyst_buy_count: Number of buy/strong-buy recommendations in the last 30 days.
        analyst_hold_count: Number of hold/neutral recommendations in the last 30 days.
        analyst_sell_count: Number of sell/strong-sell recommendations in the last 30 days.
        analyst_price_target: Mean analyst price target in USD; None if unavailable.
        news_headline: Most recent news headline from Alpaca News.
        components: Raw component scores before weighting (for audit trail).
    """
    ticker: str
    signal_type: SignalType
    confidence: float                       # 0.0 to 1.0
    order_type: Optional[OrderType] = None  # None for NEUTRAL
    sentiment_score: Optional[float] = None
    politician_action: Optional[str] = None
    politician_name: Optional[str] = None
    politician_party: Optional[str] = None
    politician_chamber: Optional[str] = None
    politician_amount: Optional[str] = None
    disclosure_url: Optional[str] = None
    analyst_rating: Optional[str] = None
    analyst_buy_count: int = 0
    analyst_hold_count: int = 0
    analyst_sell_count: int = 0
    analyst_price_target: Optional[float] = None
    news_headline: Optional[str] = None
    components: dict = field(default_factory=dict)  # raw component scores for audit


class Strategy(ABC):
    """Abstract base for all trading strategies.

    Subclasses operating on OHLCV bar data should implement generate_signal()
    with a DataFrame argument. Alternative-data strategies (like ConfluenceStrategy)
    may override with structured keyword arguments instead; that departure is
    documented in the subclass.
    """

    @abstractmethod
    def generate_signal(self, df: pd.DataFrame) -> Optional[Signal]:
        """Generate a trading signal from price bar data.

        Args:
            df: OHLCV DataFrame with at minimum a 'close' column.

        Returns:
            A Signal instance, or None if no actionable signal is found.
        """

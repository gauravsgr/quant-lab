"""Abstract broker interface and shared data types.

All broker implementations must subclass Broker and implement every abstract
method. Strategy and execution code depends only on this interface; concrete
broker classes (AlpacaBroker, BacktestBroker) are selected at startup and
injected via dependency injection.

This keeps strategy and risk logic fully broker-agnostic.
"""
from abc import ABC, abstractmethod
from dataclasses import dataclass
from typing import Optional


@dataclass
class OrderResult:
    """Standardized result returned by any broker order submission.

    Attributes:
        broker_order_id: Unique order identifier assigned by the broker.
        ticker: Symbol the order was placed for.
        qty: Number of shares or contracts ordered.
        filled_price: Average fill price; None if the order is not yet filled.
        status: Order status string (e.g., "filled", "pending", "canceled").
        order_type: Order type string (e.g., "market", "limit").
    """
    broker_order_id: str
    ticker: str
    qty: float
    filled_price: Optional[float]
    status: str
    order_type: str


class Broker(ABC):
    """Abstract interface for all broker integrations.

    Concrete implementations must provide equity orders, options orders,
    position queries, price data, and account information. All monetary
    values are in USD.
    """

    @abstractmethod
    def submit_order(
        self,
        ticker: str,
        qty: float,
        side: str,
        order_type: str = "market",
        time_in_force: str = "day",
        limit_price: Optional[float] = None,
        trail_percent: Optional[float] = None,
    ) -> OrderResult:
        """Submit an equity order and return order details.

        Args:
            ticker: Stock symbol.
            qty: Number of shares.
            side: "buy" or "sell".
            order_type: "market", "limit", or "trailing_stop".
            time_in_force: "day" or "gtc".
            limit_price: Required if order_type is "limit".
            trail_percent: Required if order_type is "trailing_stop".

        Returns:
            An OrderResult with fill details.
        """

    @abstractmethod
    def submit_options_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        order_type: str = "market",
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        """Submit an options order using an OCC contract symbol.

        Args:
            symbol: OCC option symbol (e.g., "AAPL230616C00150000").
            qty: Number of contracts.
            side: "buy" or "sell".
            order_type: "market" or "limit".
            limit_price: Required if order_type is "limit".

        Returns:
            An OrderResult with fill details.
        """

    @abstractmethod
    def get_positions(self) -> list[dict]:
        """Return all current positions as a list of dicts.

        Returns:
            List of dicts with keys: ticker, qty, market_value,
            unrealized_pl, avg_entry_price, current_price.
        """

    @abstractmethod
    def get_bars(self, ticker: str, timeframe: str = "1Day", limit: int = 30) -> list[dict]:
        """Return recent OHLCV bars for a ticker.

        Args:
            ticker: Stock symbol.
            timeframe: Bar size string (e.g., "1Day", "1Hour").
            limit: Maximum number of bars to return.

        Returns:
            List of bar dicts with keys: timestamp, open, high, low, close, volume.
        """

    @abstractmethod
    def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an open order by broker ID.

        Args:
            broker_order_id: The broker's order identifier.

        Returns:
            True if the cancellation succeeded, False otherwise.
        """

    @abstractmethod
    def get_account_equity(self) -> float:
        """Return the total account equity in USD.

        Returns:
            Account equity as a float.
        """

    @abstractmethod
    def get_latest_price(self, ticker: str) -> Optional[float]:
        """Return the most recent trade price for a ticker.

        Args:
            ticker: Stock symbol.

        Returns:
            Latest close price in USD, or None if the price cannot be fetched.
        """

    @abstractmethod
    def get_options_chain(
        self, ticker: str, option_type: str, expiry_min_days: int = 25, expiry_max_days: int = 40
    ) -> list[dict]:
        """Return available options contracts filtered by expiry and type.

        Args:
            ticker: Underlying stock symbol.
            option_type: "call" or "put".
            expiry_min_days: Minimum days to expiration (inclusive).
            expiry_max_days: Maximum days to expiration (inclusive).

        Returns:
            List of contract dicts with keys: symbol, strike, expiry, bid, ask.
        """

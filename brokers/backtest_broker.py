"""In-memory broker for backtesting.

Simulates instant order fills at the current bar's close price. Maintains
in-memory state for positions, equity, and prices. No network calls are made.

Typical usage:
    broker = BacktestBroker(initial_equity=100_000)
    broker.set_price("AAPL", 185.00)
    result = broker.submit_order("AAPL", qty=10, side="buy")
"""
import uuid
from typing import Optional

from brokers.base import Broker, OrderResult


class BacktestBroker(Broker):
    """In-memory broker that simulates order fills at the current bar close price.

    All orders fill immediately at the price last set via set_price(). There are
    no partial fills, slippage, or commissions. This is intentional for fast
    strategy evaluation; add slippage in a subclass if needed.

    Attributes:
        _equity: Current account equity in USD.
        _positions: Dict mapping ticker to position dict.
        _orders: List of all submitted order dicts (for inspection in tests).
        _prices: Dict mapping ticker to the most recently set price.
    """

    def __init__(self, initial_equity: float = 100_000.0):
        """Initialize the broker with a starting equity balance.

        Args:
            initial_equity: Starting account value in USD.
        """
        self._equity = initial_equity
        self._positions: dict[str, dict] = {}
        self._orders: list[dict] = []
        self._prices: dict[str, float] = {}

    def set_price(self, ticker: str, price: float) -> None:
        """Set the current market price for a ticker.

        Must be called before submitting any orders for that ticker.

        Args:
            ticker: Stock symbol.
            price: Current bar close price in USD.
        """
        self._prices[ticker] = price

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
        """Submit an equity order that fills instantly at the current price.

        Buys reduce equity and create a position. Sells increase equity and
        remove the position. No partial fill logic is applied.

        Args:
            ticker: Stock symbol.
            qty: Number of shares.
            side: "buy" or "sell".
            order_type: Ignored; all orders fill at market price.
            time_in_force: Ignored in backtest mode.
            limit_price: Ignored in backtest mode.
            trail_percent: Ignored in backtest mode.

        Returns:
            An OrderResult with a generated UUID as broker_order_id.
        """
        price = self._prices.get(ticker, 0.0)
        order_id = str(uuid.uuid4())
        cost = price * qty

        if side.lower() == "buy":
            self._equity -= cost
            self._positions[ticker] = {
                "qty": qty, "avg_entry_price": price, "current_price": price
            }
        else:
            self._equity += cost
            self._positions.pop(ticker, None)

        return OrderResult(
            broker_order_id=order_id,
            ticker=ticker,
            qty=qty,
            filled_price=price,
            status="filled",
            order_type=order_type,
        )

    def submit_options_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        order_type: str = "market",
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        """Submit an options order by delegating to submit_order().

        Options are treated as equity orders in backtest mode for simplicity.

        Args:
            symbol: OCC option symbol (treated as a ticker).
            qty: Number of contracts.
            side: "buy" or "sell".
            order_type: Ignored in backtest mode.
            limit_price: Ignored in backtest mode.

        Returns:
            An OrderResult from the delegated submit_order call.
        """
        return self.submit_order(symbol, float(qty), side, order_type)

    def get_positions(self) -> list[dict]:
        """Return all current positions as a list of normalized dicts.

        Returns:
            List of position dicts with keys: ticker, qty, market_value,
            unrealized_pl, avg_entry_price, current_price.
        """
        return [
            {
                "ticker": t,
                "qty": v["qty"],
                "market_value": v["qty"] * v["current_price"],
                "unrealized_pl": v["qty"] * (v["current_price"] - v["avg_entry_price"]),
                "avg_entry_price": v["avg_entry_price"],
                "current_price": v["current_price"],
            }
            for t, v in self._positions.items()
        ]

    def get_bars(self, ticker: str, timeframe: str = "1Day", limit: int = 30) -> list[dict]:
        """Return an empty list (no historical data in backtest mode).

        Args:
            ticker: Stock symbol.
            timeframe: Ignored.
            limit: Ignored.

        Returns:
            Empty list.
        """
        return []

    def cancel_order(self, broker_order_id: str) -> bool:
        """Simulate a successful order cancellation.

        Args:
            broker_order_id: The UUID of the order to cancel.

        Returns:
            Always True in backtest mode.
        """
        return True

    def get_account_equity(self) -> float:
        """Return the current account equity.

        Returns:
            Account equity in USD.
        """
        return self._equity

    def get_latest_price(self, ticker: str) -> Optional[float]:
        """Return the most recently set price for a ticker.

        Args:
            ticker: Stock symbol.

        Returns:
            Price in USD, or None if set_price() has not been called for this ticker.
        """
        return self._prices.get(ticker)

    def get_options_chain(
        self, ticker: str, option_type: str, expiry_min_days: int = 25, expiry_max_days: int = 40
    ) -> list[dict]:
        """Return an empty options chain (not simulated in backtest mode).

        Args:
            ticker: Underlying stock symbol.
            option_type: "call" or "put".
            expiry_min_days: Ignored.
            expiry_max_days: Ignored.

        Returns:
            Empty list.
        """
        return []

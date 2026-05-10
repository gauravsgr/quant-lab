"""Position sizing and trailing stop logic.

Enforces the two core risk parameters:
    MAX_POSITION_PCT  - maximum fraction of account equity in any single position (default 5%)
    TRAILING_STOP_PCT - trailing stop distance below the position's highest price (default 10%)

Both values can be overridden via environment variables without code changes.
All functions are stateless pure computations; the DB trailing-stop state is
managed by db/repository.py and updated by execution/portfolio.py.
"""
import os
from typing import Optional

TRAILING_STOP_PCT = float(os.getenv("TRAILING_STOP_PCT", "0.10"))
MAX_POSITION_PCT = float(os.getenv("MAX_POSITION_PCT", "0.05"))


def max_order_value(account_equity: float, pct: float = MAX_POSITION_PCT) -> float:
    """Compute the maximum notional value for a new position.

    Args:
        account_equity: Total account equity in USD.
        pct: Maximum position size as a fraction of equity.

    Returns:
        Maximum notional value in USD.
    """
    return account_equity * pct


def check_position_size(account_equity: float, order_value: float) -> bool:
    """Return True if the proposed order is within the position size limit.

    Args:
        account_equity: Total account equity in USD.
        order_value: Proposed notional order value in USD.

    Returns:
        True if order_value <= max_order_value(account_equity).
    """
    return order_value <= max_order_value(account_equity)


def compute_initial_stop(entry_price: float, pct: float = TRAILING_STOP_PCT) -> float:
    """Compute the initial trailing stop price for a new position.

    Args:
        entry_price: Fill price of the entry order in USD.
        pct: Trailing stop distance as a fraction (0.10 = 10% below entry).

    Returns:
        Initial stop price, rounded to 4 decimal places.
    """
    return round(entry_price * (1 - pct), 4)


def update_trailing_stop(
    entry_price: float,
    current_high: float,
    new_price: float,
    stop_pct: float = TRAILING_STOP_PCT,
) -> tuple[float, float, bool]:
    """Advance the trailing stop if price has moved to a new high.

    The stop ratchets upward as price rises but never moves downward.
    An exit is only triggered when price falls below the stop AND also
    falls below the original entry, preventing premature exits on small
    retracements above entry.

    Args:
        entry_price: Original fill price in USD.
        current_high: Highest price seen since the position was opened.
        new_price: Current market price in USD.
        stop_pct: Trailing stop distance as a fraction.

    Returns:
        A tuple of (new_high, new_stop_price, should_exit) where:
            new_high: Updated high-water mark.
            new_stop_price: Updated stop price (new_high * (1 - stop_pct)).
            should_exit: True if the position should be closed now.
    """
    new_high = max(current_high, new_price)
    new_stop = round(new_high * (1 - stop_pct), 4)
    should_exit = new_price <= new_stop and new_price < entry_price
    return new_high, new_stop, should_exit


def compute_qty_from_notional(notional: float, price: float) -> float:
    """Compute share quantity for a given notional value and price.

    Args:
        notional: Target notional order value in USD.
        price: Current price per share in USD.

    Returns:
        Quantity of shares, rounded to 4 decimal places. Returns 0.0 if price
        is zero or negative.
    """
    if price <= 0:
        return 0.0
    return round(notional / price, 4)

"""End-of-day portfolio management: mark-to-market and ghost trade recording.

Called by the afternoon cycle (3:45 PM EST) to:
    - Fetch current prices for all open orders.
    - Update trailing stop high-water marks when price makes a new high.
    - Close positions whose price has dropped below the trailing stop.
    - Record ghost trades (signals that fired but were not executed) for audit.

All DB writes go through db/repository.py.
"""
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy.engine import Connection

import db.repository as repo
from brokers.base import Broker
from execution import risk
from strategies.base import Signal


def mark_to_market(broker: Broker, db_conn: Connection, date: Optional[str] = None) -> list[dict]:
    """Fetch current prices for all open orders and update trailing stops.

    For each open order:
        - Fetches the latest price for the underlying ticker.
        - Advances the trailing stop if price makes a new high.
        - Closes the order and records realized P&L if the stop is triggered.
        - Records unrealized P&L in the performance table.

    Args:
        broker: A Broker implementation used to fetch current prices.
        db_conn: Active SQLAlchemy connection.
        date: Date string in YYYY-MM-DD format. Defaults to today (UTC).

    Returns:
        List of order dicts for positions that were closed by trailing stop.
    """
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")
    open_orders = repo.get_open_orders(db_conn)
    closed = []

    for order in open_orders:
        ticker = order["ticker"]
        underlying = _extract_underlying(ticker)
        current_price = broker.get_latest_price(underlying)

        if not current_price:
            logger.warning(f"Cannot fetch current price for {ticker}, skipping MTM")
            continue

        entry = order["entry_price"] or current_price
        high = order["trailing_stop_high"] or entry
        stop = order["stop_price"] or risk.compute_initial_stop(entry)

        new_high, new_stop, should_exit = risk.update_trailing_stop(
            entry_price=entry,
            current_high=high,
            new_price=current_price,
        )

        unrealized_pnl = (current_price - entry) * order["qty"]

        if new_high > high:
            repo.update_trailing_high(db_conn, order["id"], new_high, new_stop)

        if should_exit:
            realized_pnl = (current_price - entry) * order["qty"]
            repo.close_order(db_conn, order["id"], pnl=realized_pnl)
            logger.info(
                f"Trailing stop triggered for {ticker}: "
                f"price={current_price} stop={new_stop} pnl={realized_pnl:+.2f}"
            )
            closed.append(order)
            repo.upsert_performance(
                db_conn,
                date=date,
                ticker=ticker,
                order_id=order["id"],
                mark_price=current_price,
                unrealized_pnl=0.0,
                realized_pnl=realized_pnl,
                is_ghost=0,
            )
        else:
            repo.upsert_performance(
                db_conn,
                date=date,
                ticker=ticker,
                order_id=order["id"],
                mark_price=current_price,
                unrealized_pnl=unrealized_pnl,
                is_ghost=0,
            )

    return closed


def record_ghost_trades(
    signals: list[Signal],
    traded_tickers: set[str],
    broker: Broker,
    db_conn: Connection,
    date: Optional[str] = None,
) -> None:
    """Record performance entries for signals that fired but were not traded.

    A ghost trade represents counterfactual performance: what would the return
    have been if the signal had been executed? Used in the weekly precision audit
    to evaluate signal quality independent of execution decisions.

    Signals are skipped if they are NEUTRAL or if their ticker was already traded
    today via a real order.

    Args:
        signals: All Signal instances generated today.
        traded_tickers: Set of tickers that have real orders today.
        broker: A Broker implementation used to fetch current prices.
        db_conn: Active SQLAlchemy connection.
        date: Date string in YYYY-MM-DD format. Defaults to today (UTC).
    """
    date = date or datetime.now(timezone.utc).strftime("%Y-%m-%d")

    for signal in signals:
        if signal.ticker in traded_tickers or signal.signal_type == "NEUTRAL":
            continue
        current_price = broker.get_latest_price(signal.ticker)
        if not current_price:
            continue

        logger.info(f"Ghost trade recorded for {signal.ticker} ({signal.signal_type})")
        repo.upsert_performance(
            db_conn,
            date=date,
            ticker=signal.ticker,
            order_id=None,
            mark_price=current_price,
            unrealized_pnl=0.0,
            is_ghost=1,
        )


def _extract_underlying(ticker: str) -> str:
    """Extract the underlying symbol from an OCC option symbol.

    OCC format example: AAPL240119C00185000, where the leading alpha chars are
    the underlying symbol. Falls back to the full ticker for non-option symbols.

    Args:
        ticker: Ticker string, which may be a plain symbol or an OCC option symbol.

    Returns:
        The underlying stock symbol.
    """
    if len(ticker) > 6 and any(c.isdigit() for c in ticker):
        return "".join(c for c in ticker if c.isalpha()).rstrip("CP") or ticker
    return ticker

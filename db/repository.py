"""Database access layer for the trading agent.

All reads and writes go through this module. No other module constructs raw
SQL or calls the SQLAlchemy engine directly. Every write commits immediately
so that in-process readers see the latest state.

Sections:
    Signals     - insert and query signal rows
    Orders      - insert, query, close, and update trailing stop on orders
    Performance - upsert daily mark-to-market and ghost trade records
    Adanos      - monthly call counter for API budget enforcement
"""
from datetime import datetime, timezone
from typing import Optional

from sqlalchemy import Integer, select, update, insert, func
from sqlalchemy.engine import Connection

from db.models import (
    signals_table, orders_table, performance_table, adanos_usage_table
)


# ---------------------------------------------------------------------------
# Signals
# ---------------------------------------------------------------------------

def insert_signal(conn: Connection, **fields) -> int:
    """Insert a new signal row and return its primary key.

    Args:
        conn: Active SQLAlchemy connection.
        **fields: Column values matching signals_table. created_at defaults to
            the current UTC timestamp if not provided.

    Returns:
        Integer primary key of the newly inserted signal row.
    """
    fields.setdefault("created_at", _now())
    result = conn.execute(insert(signals_table).values(**fields))
    conn.commit()
    return result.inserted_primary_key[0]


def get_signals_for_date(conn: Connection, date: str) -> list[dict]:
    """Return all signal rows created on the given calendar date.

    Args:
        conn: Active SQLAlchemy connection.
        date: Date string in YYYY-MM-DD format.

    Returns:
        List of row dicts keyed by column name.
    """
    rows = conn.execute(
        select(signals_table).where(signals_table.c.created_at.like(f"{date}%"))
    ).fetchall()
    return [dict(r._mapping) for r in rows]


# ---------------------------------------------------------------------------
# Orders
# ---------------------------------------------------------------------------

def insert_order(conn: Connection, **fields) -> int:
    """Insert a new order row and return its primary key.

    Args:
        conn: Active SQLAlchemy connection.
        **fields: Column values matching orders_table. submitted_at defaults to
            the current UTC timestamp if not provided.

    Returns:
        Integer primary key of the newly inserted order row.
    """
    fields.setdefault("submitted_at", _now())
    result = conn.execute(insert(orders_table).values(**fields))
    conn.commit()
    return result.inserted_primary_key[0]


def get_open_orders(conn: Connection) -> list[dict]:
    """Return all orders with status 'open'.

    Args:
        conn: Active SQLAlchemy connection.

    Returns:
        List of row dicts for all open orders.
    """
    rows = conn.execute(
        select(orders_table).where(orders_table.c.status == "open")
    ).fetchall()
    return [dict(r._mapping) for r in rows]


def close_order(conn: Connection, order_id: int, pnl: float, closed_at: Optional[str] = None) -> None:
    """Mark an order as closed with its realized P&L.

    Args:
        conn: Active SQLAlchemy connection.
        order_id: Primary key of the order to close.
        pnl: Realized profit and loss in USD.
        closed_at: ISO 8601 UTC timestamp string. Defaults to now.
    """
    conn.execute(
        update(orders_table)
        .where(orders_table.c.id == order_id)
        .values(status="closed", pnl=pnl, closed_at=closed_at or _now())
    )
    conn.commit()


def update_trailing_high(conn: Connection, order_id: int, new_high: float, new_stop: float) -> None:
    """Update the trailing stop fields when price makes a new high.

    Args:
        conn: Active SQLAlchemy connection.
        order_id: Primary key of the order to update.
        new_high: New highest price seen since position was opened.
        new_stop: New trailing stop price (new_high * (1 - stop_pct)).
    """
    conn.execute(
        update(orders_table)
        .where(orders_table.c.id == order_id)
        .values(trailing_stop_high=new_high, stop_price=new_stop)
    )
    conn.commit()


def get_traded_tickers_for_date(conn: Connection, date: str) -> set[str]:
    """Return the set of tickers that have real (non-ghost) orders today.

    Used to prevent duplicate same-day trades on the same ticker.

    Args:
        conn: Active SQLAlchemy connection.
        date: Date string in YYYY-MM-DD format.

    Returns:
        Set of ticker strings traded today.
    """
    rows = conn.execute(
        select(orders_table.c.ticker).where(
            orders_table.c.submitted_at.like(f"{date}%"),
            orders_table.c.status != "ghost",
        )
    ).fetchall()
    return {r[0] for r in rows}


# ---------------------------------------------------------------------------
# Performance
# ---------------------------------------------------------------------------

def upsert_performance(conn: Connection, **fields) -> None:
    """Insert or update a daily performance record.

    The unique key is (date, ticker, is_ghost). If a matching row already
    exists it is updated in place; otherwise a new row is inserted.

    Args:
        conn: Active SQLAlchemy connection.
        **fields: Column values matching performance_table. recorded_at
            defaults to the current UTC timestamp if not provided.
    """
    fields.setdefault("recorded_at", _now())
    existing = conn.execute(
        select(performance_table).where(
            performance_table.c.date == fields["date"],
            performance_table.c.ticker == fields["ticker"],
            performance_table.c.is_ghost == fields.get("is_ghost", 0),
        )
    ).fetchone()
    if existing:
        conn.execute(
            update(performance_table)
            .where(performance_table.c.id == existing.id)
            .values(**fields)
        )
    else:
        conn.execute(insert(performance_table).values(**fields))
    conn.commit()


def get_weekly_signal_precision(conn: Connection, since: str) -> dict:
    """Return win-rate statistics grouped by order type for the given window.

    Args:
        conn: Active SQLAlchemy connection.
        since: Earliest closed_at date to include, in YYYY-MM-DD format.

    Returns:
        Dict keyed by order_type, each value containing:
            signals (int): total closed orders
            won (int): orders with positive P&L
            lost (int): orders with non-positive P&L
            win_rate (float): percentage of winning orders
    """
    rows = conn.execute(
        select(
            orders_table.c.order_type,
            func.count().label("total"),
            func.sum(
                (orders_table.c.pnl > 0).cast(Integer)
            ).label("wins"),
        )
        .where(
            orders_table.c.status == "closed",
            orders_table.c.closed_at >= since,
        )
        .group_by(orders_table.c.order_type)
    ).fetchall()

    result = {}
    for row in rows:
        total = row.total or 0
        wins = row.wins or 0
        result[row.order_type] = {
            "signals": total,
            "won": wins,
            "lost": total - wins,
            "win_rate": round(wins / total * 100, 1) if total else 0.0,
        }
    return result


def get_ghost_trades_for_week(conn: Connection, since: str) -> list[dict]:
    """Return all ghost trade performance records on or after `since`.

    Args:
        conn: Active SQLAlchemy connection.
        since: Earliest date to include, in YYYY-MM-DD format.

    Returns:
        List of row dicts for ghost performance entries.
    """
    rows = conn.execute(
        select(performance_table).where(
            performance_table.c.is_ghost == 1,
            performance_table.c.date >= since,
        )
    ).fetchall()
    return [dict(r._mapping) for r in rows]


# ---------------------------------------------------------------------------
# Adanos budget guard
# ---------------------------------------------------------------------------

def get_adanos_call_count(conn: Connection, month: str) -> int:
    """Return the number of Adanos API calls made in the given month.

    Args:
        conn: Active SQLAlchemy connection.
        month: Month string in YYYY-MM format.

    Returns:
        Integer call count; 0 if no row exists for the month yet.
    """
    row = conn.execute(
        select(adanos_usage_table.c.call_count).where(adanos_usage_table.c.month == month)
    ).fetchone()
    return row[0] if row else 0


def increment_adanos_calls(conn: Connection, month: str) -> int:
    """Increment the Adanos call counter for the given month by one.

    Inserts a new row if none exists for the month.

    Args:
        conn: Active SQLAlchemy connection.
        month: Month string in YYYY-MM format.

    Returns:
        The updated call count after the increment.
    """
    existing = conn.execute(
        select(adanos_usage_table).where(adanos_usage_table.c.month == month)
    ).fetchone()
    if existing:
        new_count = existing.call_count + 1
        conn.execute(
            update(adanos_usage_table)
            .where(adanos_usage_table.c.month == month)
            .values(call_count=new_count)
        )
    else:
        new_count = 1
        conn.execute(insert(adanos_usage_table).values(month=month, call_count=1))
    conn.commit()
    return new_count


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _now() -> str:
    """Return the current UTC time as an ISO 8601 string."""
    return datetime.now(timezone.utc).isoformat()

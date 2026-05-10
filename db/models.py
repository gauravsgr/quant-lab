"""SQLAlchemy Core table definitions for the trading agent database.

All tables are created idempotently via create_all(). The database file path
is controlled by the DB_PATH environment variable (default: trading_system.db).

Tables:
    signals            - every scored signal with all raw alternative-data inputs
    orders             - every submitted order with trailing stop state
    performance        - daily mark-to-market records and ghost trades
    adanos_usage       - monthly API call counter for Adanos budget enforcement
    pending_approvals  - signals awaiting user approval via Slack buttons
"""
import os
from sqlalchemy import (
    create_engine, MetaData, Table, Column,
    Integer, Text, Float, UniqueConstraint,
)

# SQLAlchemy exports Float; REAL is the SQLite storage type it maps to.
Real = Float

DB_PATH = os.getenv("DB_PATH", "trading_system.db")
engine = create_engine(f"sqlite:///{DB_PATH}", echo=False)
metadata = MetaData()

signals_table = Table(
    "signals", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("ticker", Text, nullable=False),
    Column("signal_type", Text, nullable=False),   # STRONG_BUY | STRONG_PUT | NEUTRAL
    Column("sentiment_score", Real),
    Column("politician_action", Text),             # BUY | SELL | None
    Column("politician_name", Text),
    Column("politician_party", Text),
    Column("politician_chamber", Text),
    Column("politician_amount", Text),
    Column("analyst_rating", Text),
    Column("analyst_buy_count", Integer),
    Column("analyst_hold_count", Integer),
    Column("analyst_sell_count", Integer),
    Column("analyst_price_target", Real),
    Column("news_headline", Text),
    Column("confidence", Real, nullable=False),
    Column("created_at", Text, nullable=False),
)

orders_table = Table(
    "orders", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("signal_id", Integer),
    Column("ticker", Text, nullable=False),
    Column("order_type", Text, nullable=False),    # call_option | put_option | equity_long | equity_short
    Column("broker_order_id", Text, unique=True),
    Column("qty", Real, nullable=False),
    Column("entry_price", Real),
    Column("stop_price", Real),
    Column("trailing_stop_high", Real),
    Column("status", Text, nullable=False),        # open | closed | ghost
    Column("pnl", Real),
    Column("submitted_at", Text, nullable=False),
    Column("closed_at", Text),
)

performance_table = Table(
    "performance", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("date", Text, nullable=False),
    Column("ticker", Text, nullable=False),
    Column("order_id", Integer),
    Column("mark_price", Real, nullable=False),
    Column("unrealized_pnl", Real, nullable=False),
    Column("realized_pnl", Real),
    Column("is_ghost", Integer, nullable=False, default=0),
    Column("recorded_at", Text, nullable=False),
    UniqueConstraint("date", "ticker", "is_ghost", name="uq_perf_date_ticker_ghost"),
)

adanos_usage_table = Table(
    "adanos_usage", metadata,
    Column("month", Text, primary_key=True),       # YYYY-MM
    Column("call_count", Integer, nullable=False, default=0),
)

pending_approvals_table = Table(
    "pending_approvals", metadata,
    Column("id", Integer, primary_key=True, autoincrement=True),
    Column("signal_id", Integer, nullable=False),
    Column("ticker", Text, nullable=False),
    Column("signal_type", Text, nullable=False),
    # JSON blob so the schema stays platform-agnostic; Slack stores ts+channel,
    # a future Telegram integration stores message_id+chat_id, etc.
    Column("notification_metadata", Text, nullable=False),
    Column("status", Text, nullable=False, default="pending"),   # pending | approved | rejected | failed
    Column("created_at", Text, nullable=False),
    Column("resolved_at", Text),
)


def create_all() -> None:
    """Create all tables if they do not already exist.

    Safe to call multiple times (idempotent). Should be called once at
    application startup before any DB reads or writes.
    """
    metadata.create_all(engine)

"""Tests for the database access layer using in-memory SQLite."""
import pytest
from sqlalchemy import create_engine
from sqlalchemy.orm import Session

from db.models import metadata, signals_table, orders_table, adanos_usage_table
import db.repository as repo


@pytest.fixture
def conn():
    """In-memory SQLite connection for isolation."""
    engine = create_engine("sqlite:///:memory:", echo=False)
    metadata.create_all(engine)
    with engine.connect() as c:
        yield c


class TestSignals:
    def test_insert_and_retrieve(self, conn):
        sid = repo.insert_signal(
            conn,
            ticker="AAPL",
            signal_type="STRONG_BUY",
            sentiment_score=0.82,
            confidence=0.84,
        )
        assert isinstance(sid, int)
        rows = repo.get_signals_for_date(conn, "2026")  # broad match via LIKE
        assert any(r["ticker"] == "AAPL" for r in rows)


class TestOrders:
    def test_insert_and_get_open(self, conn):
        oid = repo.insert_order(
            conn,
            signal_id=1,
            ticker="NVDA",
            order_type="call_option",
            broker_order_id="abc-123",
            qty=2.0,
            entry_price=150.0,
            stop_price=135.0,
            trailing_stop_high=150.0,
            status="open",
        )
        assert isinstance(oid, int)
        open_orders = repo.get_open_orders(conn)
        assert any(o["ticker"] == "NVDA" for o in open_orders)

    def test_close_order(self, conn):
        oid = repo.insert_order(
            conn,
            signal_id=1,
            ticker="TSLA",
            order_type="equity_long",
            broker_order_id="xyz-456",
            qty=5.0,
            entry_price=200.0,
            stop_price=180.0,
            trailing_stop_high=200.0,
            status="open",
        )
        repo.close_order(conn, oid, pnl=150.0)
        open_orders = repo.get_open_orders(conn)
        assert not any(o["id"] == oid for o in open_orders)


class TestAdanosBudget:
    def test_counter_starts_at_zero(self, conn):
        count = repo.get_adanos_call_count(conn, "2026-05")
        assert count == 0

    def test_increment(self, conn):
        repo.increment_adanos_calls(conn, "2026-05")
        repo.increment_adanos_calls(conn, "2026-05")
        count = repo.get_adanos_call_count(conn, "2026-05")
        assert count == 2

    def test_different_months_independent(self, conn):
        repo.increment_adanos_calls(conn, "2026-04")
        repo.increment_adanos_calls(conn, "2026-05")
        assert repo.get_adanos_call_count(conn, "2026-04") == 1
        assert repo.get_adanos_call_count(conn, "2026-05") == 1

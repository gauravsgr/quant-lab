"""Tests for the Slack Socket Mode approve/reject callback handlers.

All tests mock the DB, broker, and notifier so no real API calls are made.
"""
from unittest.mock import MagicMock, patch, call

import pytest

from strategies.base import Signal
from utils.slack_actions import _handle_approve, _handle_reject


def make_pending(status="pending"):
    return {
        "id": 1,
        "signal_id": 10,
        "ticker": "NVDA",
        "signal_type": "STRONG_BUY",
        "notification_metadata": {"platform": "slack", "ts": "1234.5", "channel": "C123"},
        "status": status,
        "created_at": "2026-05-10T10:15:00+00:00",
        "resolved_at": None,
    }


def make_signal_obj():
    return Signal(
        ticker="NVDA",
        signal_type="STRONG_BUY",
        confidence=0.84,
        order_type="call_option",
        sentiment_score=0.82,
        politician_action="BUY",
        analyst_rating="Strong Buy",
        analyst_buy_count=20,
        analyst_hold_count=3,
        analyst_sell_count=0,
    )


def make_settings():
    settings = MagicMock()
    settings.slack_app_token = "xapp-fake"
    settings.slack_bot_token = "xoxb-fake"
    return settings


def _mock_engine_ctx(conn):
    """Return a mock engine whose connect() returns conn as a context manager."""
    mock_engine = MagicMock()
    mock_engine.connect.return_value.__enter__ = MagicMock(return_value=conn)
    mock_engine.connect.return_value.__exit__ = MagicMock(return_value=False)
    return mock_engine


class TestHandleApprove:
    def test_no_pending_record_exits_early(self):
        conn = MagicMock()
        notifier = MagicMock()
        broker = MagicMock()

        with patch("db.repository.get_pending_approval", return_value=None), \
             patch("db.models.engine", _mock_engine_ctx(conn)):
            _handle_approve(99, broker, make_settings(), notifier)

        notifier.update_approval_message.assert_not_called()

    def test_already_resolved_exits_early(self):
        conn = MagicMock()
        notifier = MagicMock()
        broker = MagicMock()

        with patch("db.repository.get_pending_approval", return_value=make_pending("approved")), \
             patch("db.models.engine", _mock_engine_ctx(conn)):
            _handle_approve(10, broker, make_settings(), notifier)

        notifier.update_approval_message.assert_not_called()

    def test_signal_not_found_resolves_failed(self):
        conn = MagicMock()
        notifier = MagicMock()
        broker = MagicMock()

        with patch("db.repository.get_pending_approval", return_value=make_pending()), \
             patch("db.repository.get_signal_by_id", return_value=None), \
             patch("db.repository.resolve_pending_approval") as mock_resolve, \
             patch("db.models.engine", _mock_engine_ctx(conn)):
            _handle_approve(10, broker, make_settings(), notifier)

        mock_resolve.assert_called_once_with(conn, 10, "failed")
        notifier.update_approval_message.assert_called_once()
        assert notifier.update_approval_message.call_args.kwargs["status"] == "failed"

    def test_successful_approval_submits_order_and_updates_message(self):
        conn = MagicMock()
        notifier = MagicMock()
        broker = MagicMock()
        broker.get_account_equity.return_value = 100_000.0

        with patch("db.repository.get_pending_approval", return_value=make_pending()), \
             patch("db.repository.get_signal_by_id", return_value=make_signal_obj()), \
             patch("db.repository.resolve_pending_approval") as mock_resolve, \
             patch("execution.runner.submit_signal_order", return_value=42) as mock_submit, \
             patch("db.models.engine", _mock_engine_ctx(conn)):
            _handle_approve(10, broker, make_settings(), notifier)

        mock_submit.assert_called_once()
        # require_approval must be False so the order is actually submitted
        assert mock_submit.call_args.kwargs["require_approval"] is False
        # notifier must not be passed to avoid a second Slack message
        assert mock_submit.call_args.kwargs["notifier"] is None

        mock_resolve.assert_called_once_with(conn, 10, "approved")
        notifier.update_approval_message.assert_called_once()
        call_kwargs = notifier.update_approval_message.call_args.kwargs
        assert call_kwargs["status"] == "approved"
        assert call_kwargs["order_id"] == 42

    def test_failed_submission_resolves_as_failed(self):
        conn = MagicMock()
        notifier = MagicMock()
        broker = MagicMock()
        broker.get_account_equity.return_value = 100_000.0

        with patch("db.repository.get_pending_approval", return_value=make_pending()), \
             patch("db.repository.get_signal_by_id", return_value=make_signal_obj()), \
             patch("db.repository.resolve_pending_approval") as mock_resolve, \
             patch("execution.runner.submit_signal_order", return_value=None), \
             patch("db.models.engine", _mock_engine_ctx(conn)):
            _handle_approve(10, broker, make_settings(), notifier)

        mock_resolve.assert_called_once_with(conn, 10, "failed")
        assert notifier.update_approval_message.call_args.kwargs["status"] == "failed"

    def test_broker_exception_resolves_as_failed(self):
        conn = MagicMock()
        notifier = MagicMock()
        broker = MagicMock()
        broker.get_account_equity.side_effect = RuntimeError("broker down")

        with patch("db.repository.get_pending_approval", return_value=make_pending()), \
             patch("db.repository.get_signal_by_id", return_value=make_signal_obj()), \
             patch("db.repository.resolve_pending_approval") as mock_resolve, \
             patch("db.models.engine", _mock_engine_ctx(conn)):
            _handle_approve(10, broker, make_settings(), notifier)

        mock_resolve.assert_called_once_with(conn, 10, "failed")
        assert notifier.update_approval_message.call_args.kwargs["status"] == "failed"


class TestHandleReject:
    def test_no_pending_record_exits_early(self):
        conn = MagicMock()
        notifier = MagicMock()

        with patch("db.repository.get_pending_approval", return_value=None), \
             patch("db.models.engine", _mock_engine_ctx(conn)):
            _handle_reject(99, make_settings(), notifier)

        notifier.update_approval_message.assert_not_called()

    def test_already_resolved_exits_early(self):
        conn = MagicMock()
        notifier = MagicMock()

        with patch("db.repository.get_pending_approval", return_value=make_pending("rejected")), \
             patch("db.models.engine", _mock_engine_ctx(conn)):
            _handle_reject(10, make_settings(), notifier)

        notifier.update_approval_message.assert_not_called()

    def test_successful_rejection(self):
        conn = MagicMock()
        notifier = MagicMock()

        with patch("db.repository.get_pending_approval", return_value=make_pending()), \
             patch("db.repository.resolve_pending_approval") as mock_resolve, \
             patch("db.models.engine", _mock_engine_ctx(conn)):
            _handle_reject(10, make_settings(), notifier)

        mock_resolve.assert_called_once_with(conn, 10, "rejected")
        notifier.update_approval_message.assert_called_once()
        assert notifier.update_approval_message.call_args.kwargs["status"] == "rejected"

"""Tests for Slack Block Kit payload construction.

All tests use mocked WebClient; no real Slack API calls are made.
"""
import pytest
from unittest.mock import patch, MagicMock

from strategies.base import Signal
from utils.notifier import (
    SlackNotifier, _build_signal_blocks, _build_approval_buttons,
    _build_weekly_report_blocks, _reddit_url, _google_news_url,
    _capitol_trades_url, _bar,
)


def make_signal(signal_type="STRONG_BUY", **kwargs) -> Signal:
    defaults = dict(
        ticker="NVDA",
        signal_type=signal_type,
        confidence=0.84,
        order_type="call_option" if signal_type == "STRONG_BUY" else "put_option",
        sentiment_score=0.82,
        politician_action="BUY",
        politician_name="Sen. Test User",
        politician_party="R",
        politician_chamber="Senate",
        politician_amount="$50,001-$100,000",
        disclosure_url="https://capitoltrades.com/trades/123",
        analyst_rating="Strong Buy",
        analyst_buy_count=28,
        analyst_hold_count=4,
        analyst_sell_count=0,
        analyst_price_target=146.50,
        news_headline="NVDA beats Q1 estimates",
    )
    defaults.update(kwargs)
    return Signal(**defaults)


class TestBlockConstruction:
    def test_strong_buy_header_contains_ticker(self):
        blocks = _build_signal_blocks(make_signal("STRONG_BUY"), order=None, approval_pending=False)
        header = next(b for b in blocks if b.get("type") == "header")
        assert "NVDA" in header["text"]["text"]
        assert "STRONG BUY" in header["text"]["text"]

    def test_strong_put_header(self):
        blocks = _build_signal_blocks(make_signal("STRONG_PUT"), order=None, approval_pending=False)
        header = next(b for b in blocks if b.get("type") == "header")
        assert "STRONG PUT" in header["text"]["text"]

    def test_approval_pending_flag(self):
        blocks = _build_signal_blocks(make_signal(), order=None, approval_pending=True)
        header = next(b for b in blocks if b.get("type") == "header")
        assert "AWAITING APPROVAL" in header["text"]["text"]

    def test_research_links_present(self):
        blocks = _build_signal_blocks(make_signal(), order=None, approval_pending=False)
        link_block = next(
            b for b in blocks
            if b.get("type") == "section"
            and "RESEARCH LINKS" in b.get("text", {}).get("text", "")
        )
        assert "Reddit" in link_block["text"]["text"]
        assert "Google News" in link_block["text"]["text"]
        assert "Capitol Trades" in link_block["text"]["text"]

    def test_order_details_included_when_order_present(self):
        order = {"qty": 2, "entry_price": 125.50, "stop_price": 112.95, "notional": 5000}
        blocks = _build_signal_blocks(make_signal(), order=order, approval_pending=False)
        text_blocks = [b for b in blocks if b.get("type") == "section"]
        all_text = " ".join(b["text"]["text"] for b in text_blocks)
        assert "125.50" in all_text

    def test_weekly_report_has_table(self):
        precision = {
            "call_option": {"signals": 3, "won": 2, "lost": 1, "win_rate": 66.7},
        }
        blocks = _build_weekly_report_blocks(precision, {"call_count": 50}, [], None)
        section = next(b for b in blocks if b.get("type") == "section")
        assert "call_option" in section["text"]["text"].lower() or "call" in section["text"]["text"].lower()


class TestHelpers:
    def test_bar_full(self):
        assert _bar(1.0) == "██████████"

    def test_bar_empty(self):
        assert _bar(0.0) == "░░░░░░░░░░"

    def test_bar_half(self):
        b = _bar(0.5)
        assert "█" in b and "░" in b

    def test_reddit_url_contains_ticker(self):
        url = _reddit_url("NVDA")
        assert "NVDA" in url

    def test_google_news_url(self):
        url = _google_news_url("AAPL")
        assert "AAPL" in url


class TestSlackNotifierSend:
    def test_send_does_not_raise(self):
        with patch("slack_sdk.WebClient.chat_postMessage") as mock_post:
            mock_post.return_value = {"ts": "1234.5678", "channel": "C123"}
            notifier = SlackNotifier("xoxb-fake-token", "C123")
            result = notifier.send_signal_alert(make_signal())
            mock_post.assert_called_once()
            assert result is not None
            assert result["platform"] == "slack"

    def test_approval_pending_returns_metadata(self):
        with patch("slack_sdk.WebClient.chat_postMessage") as mock_post:
            mock_post.return_value = {"ts": "9999.0001", "channel": "C456"}
            notifier = SlackNotifier("xoxb-fake-token", "C456")
            result = notifier.send_signal_alert(make_signal(), approval_pending=True, signal_id=42)
            assert result["ts"] == "9999.0001"
            assert result["channel"] == "C456"

    def test_slack_api_error_returns_none(self):
        from slack_sdk.errors import SlackApiError
        with patch("slack_sdk.WebClient.chat_postMessage", side_effect=SlackApiError("err", {})):
            notifier = SlackNotifier("xoxb-fake-token", "C123")
            result = notifier.send_signal_alert(make_signal())
            assert result is None

    def test_update_approval_message_approved(self):
        with patch("slack_sdk.WebClient.chat_update") as mock_update:
            notifier = SlackNotifier("xoxb-fake-token", "C123")
            notifier.update_approval_message(
                {"platform": "slack", "ts": "1234.5678", "channel": "C123"},
                status="approved",
                order_id=99,
            )
            mock_update.assert_called_once()
            call_kwargs = mock_update.call_args.kwargs
            assert "99" in call_kwargs["text"]
            assert call_kwargs["ts"] == "1234.5678"

    def test_update_approval_message_rejected(self):
        with patch("slack_sdk.WebClient.chat_update") as mock_update:
            notifier = SlackNotifier("xoxb-fake-token", "C123")
            notifier.update_approval_message(
                {"platform": "slack", "ts": "5678.0", "channel": "C123"},
                status="rejected",
            )
            mock_update.assert_called_once()
            assert "REJECTED" in mock_update.call_args.kwargs["text"]

    def test_update_approval_message_no_ts_skips_call(self):
        with patch("slack_sdk.WebClient.chat_update") as mock_update:
            notifier = SlackNotifier("xoxb-fake-token", "C123")
            notifier.update_approval_message({"platform": "slack"}, status="approved")
            mock_update.assert_not_called()


class TestApprovalButtons:
    def test_buttons_block_has_two_elements(self):
        blocks = _build_approval_buttons(signal_id=7)
        assert len(blocks) == 1
        assert blocks[0]["type"] == "actions"
        elements = blocks[0]["elements"]
        assert len(elements) == 2

    def test_approve_button_action_id(self):
        blocks = _build_approval_buttons(signal_id=7)
        approve_btn = blocks[0]["elements"][0]
        assert approve_btn["action_id"] == "approve_signal"
        assert approve_btn["value"] == "7"
        assert approve_btn["style"] == "primary"

    def test_reject_button_action_id(self):
        blocks = _build_approval_buttons(signal_id=7)
        reject_btn = blocks[0]["elements"][1]
        assert reject_btn["action_id"] == "reject_signal"
        assert reject_btn["value"] == "7"
        assert reject_btn["style"] == "danger"

    def test_signal_id_embedded_as_string(self):
        blocks = _build_approval_buttons(signal_id=42)
        for element in blocks[0]["elements"]:
            assert element["value"] == "42"

    def test_approval_pending_includes_buttons(self):
        signal = make_signal()
        blocks = _build_signal_blocks(signal, order=None, approval_pending=True)
        blocks += _build_approval_buttons(signal_id=1)
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert len(action_blocks) == 1

    def test_no_approval_no_buttons(self):
        signal = make_signal()
        blocks = _build_signal_blocks(signal, order=None, approval_pending=False)
        action_blocks = [b for b in blocks if b.get("type") == "actions"]
        assert len(action_blocks) == 0

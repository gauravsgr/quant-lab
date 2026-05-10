"""Tests for ConfluenceStrategy signal generation and confidence scoring."""
import pytest
from strategies.confluence import ConfluenceStrategy, ConfluenceConfig


@pytest.fixture
def cfg():
    return ConfluenceConfig(
        sentiment_weight=0.40,
        politician_weight=0.35,
        analyst_weight=0.25,
        min_confidence=0.65,
        strong_buy_threshold=0.70,
        strong_put_threshold=-0.70,
    )


@pytest.fixture
def strategy(cfg):
    return ConfluenceStrategy(cfg)


class TestConfidenceScore:
    def test_perfect_strong_buy(self, strategy):
        sig = strategy.generate_signal(
            ticker="AAPL",
            sentiment_score=0.90,
            politician_action="BUY",
            analyst_rating="Strong Buy",
        )
        assert sig.signal_type == "STRONG_BUY"
        assert sig.order_type == "call_option"
        # 0.9*0.4 + 1.0*0.35 + 1.0*0.25 = 0.36 + 0.35 + 0.25 = 0.96
        assert sig.confidence == pytest.approx(0.96, abs=0.01)

    def test_perfect_strong_put(self, strategy):
        sig = strategy.generate_signal(
            ticker="AAPL",
            sentiment_score=-0.90,
            politician_action="SELL",
            analyst_rating="Strong Sell",
        )
        assert sig.signal_type == "STRONG_PUT"
        assert sig.order_type == "put_option"

    def test_neutral_mixed_signals(self, strategy):
        sig = strategy.generate_signal(
            ticker="AAPL",
            sentiment_score=0.50,
            politician_action="BUY",
            analyst_rating="Hold",
        )
        assert sig.signal_type == "NEUTRAL"
        assert sig.order_type is None

    def test_no_signals_defaults_neutral(self, strategy):
        sig = strategy.generate_signal(ticker="AAPL")
        assert sig.signal_type == "NEUTRAL"
        assert sig.confidence == pytest.approx(0.5, abs=0.1)

    def test_confidence_bounded(self, strategy):
        for sentiment in [-1.0, 0.0, 0.5, 1.0]:
            sig = strategy.generate_signal(
                ticker="AAPL", sentiment_score=sentiment, analyst_rating="Strong Buy"
            )
            assert 0.0 <= sig.confidence <= 1.0

    def test_politician_buy_without_sentiment_not_strong_buy(self, strategy):
        sig = strategy.generate_signal(
            ticker="AAPL",
            sentiment_score=0.50,  # below 0.70 threshold
            politician_action="BUY",
            analyst_rating="Strong Buy",
        )
        assert sig.signal_type == "NEUTRAL"

    def test_strong_put_triggered_by_analyst_alone(self, strategy):
        # Sentiment below threshold + analyst downgrade triggers STRONG_PUT
        sig = strategy.generate_signal(
            ticker="AAPL",
            sentiment_score=-0.80,
            politician_action=None,
            analyst_rating="Strong Sell",
        )
        assert sig.signal_type == "STRONG_PUT"

    def test_signal_stores_metadata(self, strategy):
        sig = strategy.generate_signal(
            ticker="NVDA",
            sentiment_score=0.82,
            politician_action="BUY",
            politician_name="Sen. Test",
            analyst_rating="Strong Buy",
            news_headline="Test headline",
        )
        assert sig.ticker == "NVDA"
        assert sig.politician_name == "Sen. Test"
        assert sig.news_headline == "Test headline"
        assert "sentiment" in sig.components

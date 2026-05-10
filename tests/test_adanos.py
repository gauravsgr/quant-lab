"""Tests for the Adanos client budget guard and response parsing."""
import pytest
from unittest.mock import patch, MagicMock
from sqlalchemy import create_engine

from db.models import metadata
import db.repository as repo
from data.loaders.adanos import AdanosClient, BudgetExhausted, _parse_adanos_response, BUDGET_LIMIT


@pytest.fixture
def conn():
    engine = create_engine("sqlite:///:memory:", echo=False)
    metadata.create_all(engine)
    with engine.connect() as c:
        yield c


class TestBudgetGuard:
    def test_budget_exhausted_at_limit(self, conn):
        month = "2026-05"
        for _ in range(BUDGET_LIMIT):
            repo.increment_adanos_calls(conn, month)

        client = AdanosClient("fake-key", conn)
        with pytest.raises(BudgetExhausted):
            client.get_buzzing_tickers()

    def test_budget_not_exhausted_below_limit(self, conn):
        month = "2026-05"
        for _ in range(BUDGET_LIMIT - 1):
            repo.increment_adanos_calls(conn, month)

        client = AdanosClient("fake-key", conn)
        with patch("requests.get") as mock_get:
            mock_get.return_value = MagicMock(
                status_code=200,
                json=lambda: [{"ticker": "AAPL", "sentiment_score": 0.8, "rank": 1}],
                raise_for_status=lambda: None,
            )
            result = client.get_buzzing_tickers()
        assert len(result) == 1
        assert result[0]["ticker"] == "AAPL"


class TestParseResponse:
    def test_list_format(self):
        data = [{"ticker": "NVDA", "sentiment_score": 0.85, "rank": 1, "source_count": 500}]
        result = _parse_adanos_response(data)
        assert result[0]["ticker"] == "NVDA"
        assert result[0]["sentiment_score"] == pytest.approx(0.85)

    def test_dict_with_data_key(self):
        data = {"data": [{"ticker": "TSLA", "sentiment_score": -0.6}]}
        result = _parse_adanos_response(data)
        assert result[0]["ticker"] == "TSLA"

    def test_score_clamped_to_range(self):
        data = [{"ticker": "AAPL", "sentiment_score": 2.5}]
        result = _parse_adanos_response(data)
        assert result[0]["sentiment_score"] == 1.0

    def test_skips_items_without_ticker(self):
        data = [{"sentiment_score": 0.5}]
        result = _parse_adanos_response(data)
        assert result == []

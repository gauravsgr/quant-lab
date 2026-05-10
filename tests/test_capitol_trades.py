"""Tests for the Capitol Trades HTML scraper and date parsing helpers."""
import pytest
from bs4 import BeautifulSoup
from data.loaders.capitol_trades import _parse_trades_table, _looks_like_date, _parse_date


SAMPLE_HTML = """
<table>
  <tr><th>Politician</th><th>Chamber</th><th>Ticker</th><th>Type</th><th>Amount</th><th>Date</th></tr>
  <tr>
    <td>Sen. Jane Doe (R)</td>
    <td>Senate</td>
    <td><a href="/trades/1">NVDA</a></td>
    <td>Purchase</td>
    <td>$50,001-$100,000</td>
    <td>2026-05-08</td>
  </tr>
  <tr>
    <td>Rep. John Smith (D)</td>
    <td>House</td>
    <td>AAPL</td>
    <td>Sale</td>
    <td>$15,001-$50,000</td>
    <td>2026-05-07</td>
  </tr>
</table>
"""

EMPTY_HTML = "<div>No trades found</div>"


class TestParser:
    def test_parses_buy_trade(self):
        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        rows = _parse_trades_table(soup)
        nvda_row = next((r for r in rows if r["ticker"] == "NVDA"), None)
        assert nvda_row is not None
        assert nvda_row["action"] == "buy"

    def test_parses_sell_trade(self):
        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        rows = _parse_trades_table(soup)
        aapl_row = next((r for r in rows if r["ticker"] == "AAPL"), None)
        assert aapl_row is not None
        assert aapl_row["action"] == "sell"

    def test_empty_table_returns_empty_list(self):
        soup = BeautifulSoup(EMPTY_HTML, "html.parser")
        rows = _parse_trades_table(soup)
        assert rows == []

    def test_all_rows_have_required_fields(self):
        soup = BeautifulSoup(SAMPLE_HTML, "html.parser")
        rows = _parse_trades_table(soup)
        for row in rows:
            assert "ticker" in row
            assert "action" in row
            assert "disclosure_url" in row


class TestHelpers:
    def test_date_iso_format(self):
        assert _looks_like_date("2026-05-08") is True

    def test_date_slash_format(self):
        assert _looks_like_date("05/08/2026") is True

    def test_non_date(self):
        assert _looks_like_date("NVDA") is False
        assert _looks_like_date("$50,000") is False

    def test_parse_date_iso(self):
        dt = _parse_date("2026-05-08")
        assert dt is not None
        assert dt.year == 2026

    def test_parse_date_slash(self):
        dt = _parse_date("05/08/2026")
        assert dt is not None
        assert dt.month == 5

    def test_parse_date_empty(self):
        assert _parse_date("") is None

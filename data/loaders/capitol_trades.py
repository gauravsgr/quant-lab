"""Capitol Trades scraper for STOCK Act congressional disclosure data.

Scrapes capitoltrades.com to retrieve recent politician stock trades. No API key
is needed. The scraper uses User-Agent rotation (fake_useragent), a polite
inter-request delay, and exponential backoff via tenacity.

The scraper heuristically identifies table columns by content rather than relying
on fixed column positions, because the Capitol Trades HTML layout may change.

Typical usage:
    scraper = CapitolTradesScraper()
    trades = scraper.get_recent_trades(days_back=3)
    # Returns a list of dicts with ticker, politician, action, amount, etc.
"""
import time
import random
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

BASE_URL = "https://capitoltrades.com"
_ua = UserAgent()


def _random_headers() -> dict:
    """Build an HTTP request header dict with a randomized User-Agent.

    Rotating User-Agent strings reduces the chance of being blocked by
    bot-detection heuristics on the Capitol Trades site.

    Returns:
        Dict of HTTP headers suitable for requests.get().
    """
    return {
        "User-Agent": _ua.random,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate, br",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


class CapitolTradesScraper:
    """Scraper for recent congressional stock trade disclosures.

    Fetches paginated HTML from capitoltrades.com and parses each row
    heuristically to extract ticker, action, politician metadata, and dates.

    Attributes:
        _delay: Base seconds to sleep between page requests (plus random jitter).
    """

    def __init__(self, request_delay: float = 2.0):
        """Initialize the scraper with a configurable inter-request delay.

        Args:
            request_delay: Minimum seconds to wait between page fetches.
                A random jitter of 0-1 seconds is added on top of this.
        """
        self._delay = request_delay

    @retry(
        retry=retry_if_exception_type((requests.HTTPError, requests.ConnectionError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=16),
    )
    def _fetch_page(self, url: str, params: Optional[dict] = None) -> BeautifulSoup:
        """Fetch a single HTML page and return a parsed BeautifulSoup object.

        Sleeps for _delay + random jitter before each request to be polite.
        Retries up to 3 times with exponential backoff on HTTP or connection errors.
        A 429 (rate limited) response is treated as an HTTPError to trigger retry.

        Args:
            url: Full URL to fetch.
            params: Optional query parameters to append to the URL.

        Returns:
            A BeautifulSoup object parsed from the response HTML.

        Raises:
            requests.HTTPError: On 4xx/5xx responses or 429 rate limiting.
            requests.ConnectionError: On network-level connection failures.
        """
        time.sleep(self._delay + random.uniform(0, 1))
        resp = requests.get(url, headers=_random_headers(), params=params, timeout=20)
        if resp.status_code == 429:
            logger.warning("Capitol Trades rate limited (429), backing off")
            raise requests.HTTPError(response=resp)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")

    def get_recent_trades(self, days_back: int = 3) -> list[dict]:
        """Scrape Capitol Trades for politician stock disclosures from recent days.

        Fetches pages until it encounters a disclosure older than `days_back` days,
        or until there is no "Next" pagination link.

        Args:
            days_back: Number of calendar days to look back (default 3).

        Returns:
            List of trade dicts, each containing:
                ticker (str): Stock symbol.
                politician (str): Full name of the disclosing politician.
                party (str): Party abbreviation ("R", "D", "I", or "Unknown").
                chamber (str): "Senate", "House", or "Unknown".
                action (str): "buy" or "sell".
                amount_range (str): Dollar range from the disclosure.
                disclosure_date (str): Date the disclosure was filed.
                trade_date (str): Date the trade occurred (may equal disclosure_date).
                disclosure_url (str): URL to the Capitol Trades disclosure page.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        results = []
        page = 1

        while True:
            try:
                soup = self._fetch_page(f"{BASE_URL}/trades", params={"page": page})
            except Exception as e:
                logger.error(f"Capitol Trades scrape failed on page {page}: {e}")
                break

            rows = _parse_trades_table(soup)
            if not rows:
                break

            page_done = False
            for row in rows:
                trade_date = _parse_date(row.get("disclosure_date", ""))
                if trade_date and trade_date < cutoff:
                    page_done = True
                    break
                if row.get("ticker"):
                    results.append(row)

            if page_done:
                break

            next_btn = soup.find("a", {"rel": "next"}) or soup.find("a", string="Next")
            if not next_btn:
                break
            page += 1

        logger.info(f"Capitol Trades: scraped {len(results)} trades from last {days_back} days")
        return results


def _parse_trades_table(soup: BeautifulSoup) -> list[dict]:
    """Parse all trade rows from a Capitol Trades HTML page.

    Locates the first <table> or a div with "trade" in its class, then
    extracts each data row (skipping the header).

    Args:
        soup: Parsed BeautifulSoup object for a Capitol Trades page.

    Returns:
        List of raw trade row dicts. Rows that cannot be parsed are skipped.
    """
    rows = []
    table = soup.find("table") or soup.find("div", class_=lambda c: c and "trade" in c.lower())
    if not table:
        return rows

    for tr in table.find_all("tr")[1:]:  # skip header row
        cols = tr.find_all(["td", "th"])
        if len(cols) < 4:
            continue
        try:
            row = _extract_row(cols, tr)
            if row:
                rows.append(row)
        except Exception:
            continue

    return rows


def _extract_row(cols: list, tr) -> Optional[dict]:
    """Extract a single trade row from a list of table cells.

    Uses content heuristics to identify which cell contains the ticker,
    politician name, action, amount, date, and party/chamber information.
    The Capitol Trades table layout is not guaranteed to have fixed columns.

    Args:
        cols: List of BeautifulSoup tag objects for the row's <td> cells.
        tr: The parent <tr> tag (unused but kept for future link extraction).

    Returns:
        A trade dict if both ticker and action can be identified, else None.
    """
    ticker = None
    politician = None
    action = None
    amount_range = None
    disclosure_date = None
    trade_date = None
    party = None
    chamber = None
    disclosure_url = BASE_URL

    for i, cell in enumerate(cols):
        cell_text = cell.get_text(strip=True)

        # Tickers are all-caps alpha strings of 1-5 characters.
        if not ticker and len(cell_text) <= 5 and cell_text.isupper() and cell_text.isalpha():
            ticker = cell_text

        if not action:
            lower = cell_text.lower()
            if "purchase" in lower or "buy" in lower:
                action = "buy"
            elif "sale" in lower or "sell" in lower:
                action = "sell"

        if not amount_range and "$" in cell_text:
            amount_range = cell_text

        if not disclosure_date and _looks_like_date(cell_text):
            disclosure_date = cell_text

        if not party:
            if "(R)" in cell_text or "Republican" in cell_text:
                party = "R"
            elif "(D)" in cell_text or "Democrat" in cell_text:
                party = "D"
            elif "(I)" in cell_text:
                party = "I"

        if not chamber:
            lower = cell_text.lower()
            if "senate" in lower or "senator" in lower:
                chamber = "Senate"
            elif "house" in lower or "representative" in lower or "rep." in lower:
                chamber = "House"

        # Politician names have multiple words and are not tickers, amounts, or dates.
        if not politician and len(cell_text.split()) >= 2 and "$" not in cell_text:
            candidate = cell_text
            if not _looks_like_date(candidate) and not candidate.isupper():
                politician = candidate

        link = cell.find("a", href=True)
        if link and "/trades/" in link.get("href", ""):
            disclosure_url = BASE_URL + link["href"]

    if not ticker or not action:
        return None

    return {
        "ticker": ticker,
        "politician": politician or "Unknown",
        "party": party or "Unknown",
        "chamber": chamber or "Unknown",
        "action": action,
        "amount_range": amount_range or "Unknown",
        "disclosure_date": disclosure_date or "",
        "trade_date": trade_date or disclosure_date or "",
        "disclosure_url": disclosure_url,
    }


def _looks_like_date(text: str) -> bool:
    """Return True if the text matches a recognized date format.

    Recognized formats: YYYY-MM-DD and MM/DD/YYYY.

    Args:
        text: String to test.

    Returns:
        True if the string matches a date pattern, False otherwise.
    """
    import re
    return bool(re.match(r"^\d{4}-\d{2}-\d{2}$", text) or re.match(r"^\d{2}/\d{2}/\d{4}$", text))


def _parse_date(text: str) -> Optional[datetime]:
    """Parse a date string into a timezone-aware UTC datetime.

    Supports YYYY-MM-DD and MM/DD/YYYY formats.

    Args:
        text: Date string to parse.

    Returns:
        A UTC datetime, or None if the string is empty or cannot be parsed.
    """
    import re
    if not text:
        return None
    try:
        if re.match(r"^\d{4}-\d{2}-\d{2}$", text):
            return datetime.strptime(text, "%Y-%m-%d").replace(tzinfo=timezone.utc)
        if re.match(r"^\d{2}/\d{2}/\d{4}$", text):
            return datetime.strptime(text, "%m/%d/%Y").replace(tzinfo=timezone.utc)
    except ValueError:
        pass
    return None

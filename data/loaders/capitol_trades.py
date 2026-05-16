"""Capitol Trades scraper for STOCK Act congressional disclosure data.

Scrapes capitoltrades.com to retrieve recent politician stock trades. No API key
is needed. The scraper uses User-Agent rotation (fake_useragent), a polite
inter-request delay, and exponential backoff via tenacity.

Two-phase approach required by the site's Next.js SPA architecture:
  Phase 1 — Trade ID collection: The list page (/trades?page=N) serves
    server-rendered HTML with 12 disclosure link anchors per page. The actual
    trade row data (politician, ticker, amount) is JavaScript-rendered and not
    accessible via plain requests. We collect trade IDs from these anchors only.

  Phase 2 — Detail page parsing: Each /trades/{id} page IS server-rendered
    and contains all trade fields in the static HTML. Page title format:
      "{Politician} {bought|sold} {Company} ({TICKER}:US) on {YYYY-MM-DD}"
    Body text contains: action, amount range, party/chamber, dates (Traded /
    Published).

Pagination stops when the oldest Published date on the current batch of detail
pages falls before the requested cutoff.

Typical usage:
    scraper = CapitolTradesScraper()
    trades = scraper.get_recent_trades(days_back=3)
    # Returns a list of dicts with ticker, politician, action, amount, etc.
"""
import re
import time
import random
from concurrent.futures import ThreadPoolExecutor, as_completed
from datetime import datetime, timedelta, timezone
from typing import Optional

import requests
from bs4 import BeautifulSoup
from fake_useragent import UserAgent
from loguru import logger
from tenacity import retry, stop_after_attempt, wait_exponential, retry_if_exception_type

BASE_URL = "https://www.capitoltrades.com"
_ua = UserAgent()

# Title pattern: "John Boozman sold DTE Energy Co (DTE:US) on 2025-03-19"
_TITLE_RE = re.compile(
    r"^(?P<politician>.+?)\s+(?P<verb>bought|sold|purchased|exchanged)\s+.+?"
    r"\((?P<ticker>[A-Z]{1,5}):(?:[A-Z]{2})\)\s+on\s+(?P<trade_date>\d{4}-\d{2}-\d{2})",
    re.IGNORECASE,
)


def _random_headers() -> dict:
    return {
        "User-Agent": _ua.random,
        "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
        "Accept-Language": "en-US,en;q=0.5",
        "Accept-Encoding": "gzip, deflate",
        "Connection": "keep-alive",
        "Upgrade-Insecure-Requests": "1",
    }


class CapitolTradesScraper:
    """Scraper for recent congressional stock trade disclosures.

    Uses a two-phase approach:
      1. Collect trade IDs from list page static HTML (12 per page).
      2. Fetch and parse each trade detail page (server-rendered, all fields).

    Attributes:
        _delay: Base seconds to sleep between page requests (plus random jitter).
    """

    def __init__(self, request_delay: float = 0.3):
        self._delay = request_delay

    @retry(
        retry=retry_if_exception_type((requests.HTTPError, requests.ConnectionError)),
        stop=stop_after_attempt(3),
        wait=wait_exponential(multiplier=1, min=2, max=16),
    )
    def _fetch_page(self, url: str, params: Optional[dict] = None) -> BeautifulSoup:
        time.sleep(self._delay + random.uniform(0, 0.2))
        resp = requests.get(url, headers=_random_headers(), params=params, timeout=20)
        if resp.status_code == 429:
            logger.warning("Capitol Trades rate limited (429), backing off")
            raise requests.HTTPError(response=resp)
        resp.raise_for_status()
        return BeautifulSoup(resp.text, "html.parser")

    def _get_trade_ids_from_list_page(self, page: int) -> list[str]:
        """Fetch the list page and return the 12 trade IDs in the static HTML.

        The list page serves anchor tags pointing to /trades/{id} in its
        server-rendered HTML even though the row data is JS-rendered. These
        anchors are the pagination unit — each page yields 12 unique IDs.

        Args:
            page: 1-based page number.

        Returns:
            List of trade ID strings (numeric). Empty list on error or no results.
        """
        try:
            soup = self._fetch_page(f"{BASE_URL}/trades", params={"page": page})
        except Exception as e:
            logger.error(f"Capitol Trades list page {page} failed: {e}")
            return []

        links = soup.find_all("a", href=re.compile(r"^/trades/\d+$"))
        ids = list(dict.fromkeys(link["href"].split("/")[-1] for link in links))
        logger.debug(f"Capitol Trades page {page}: found {len(ids)} trade IDs")
        return ids

    def _fetch_details_parallel(self, trade_ids: list[str], max_workers: int = 6) -> list[dict]:
        """Fetch multiple trade detail pages concurrently.

        Each worker still respects the per-request sleep delay, so the effective
        throughput is `max_workers * (1 / delay)` requests/second rather than
        the sequential `1 / delay`.

        Args:
            trade_ids: List of numeric trade ID strings to fetch.
            max_workers: Maximum concurrent detail-page fetches.

        Returns:
            List of parsed trade dicts (excludes None results for ETF/bond trades).
        """
        results = []
        with ThreadPoolExecutor(max_workers=max_workers) as executor:
            futures = {executor.submit(self._fetch_trade_detail, tid): tid for tid in trade_ids}
            for future in as_completed(futures):
                result = future.result()
                if result:
                    results.append(result)
        return results

    def _fetch_trade_detail(self, trade_id: str) -> Optional[dict]:
        """Fetch a trade detail page and parse all fields from server-rendered HTML.

        Args:
            trade_id: Numeric string trade ID from the /trades/{id} URL.

        Returns:
            Trade dict, or None if parsing fails.
        """
        url = f"{BASE_URL}/trades/{trade_id}"
        try:
            soup = self._fetch_page(url)
        except Exception as e:
            logger.warning(f"Capitol Trades detail page {trade_id} failed: {e}")
            return None
        return _parse_detail_page(soup, url)

    def get_recent_trades(self, days_back: int = 3) -> list[dict]:
        """Scrape Capitol Trades for politician stock disclosures from recent days.

        Fetches list pages to collect trade IDs, then fetches each detail page.
        Stops when the oldest Published date in the current batch predates the cutoff.

        Args:
            days_back: Number of calendar days to look back (default 3).
                Uses the "Published" (disclosure filing) date, not the trade date.

        Returns:
            List of trade dicts, each containing:
                ticker (str): Stock symbol (no country suffix).
                politician (str): Full name of the disclosing politician.
                party (str): "R", "D", "I", or "Unknown".
                chamber (str): "Senate", "House", or "Unknown".
                action (str): "buy" or "sell".
                amount_range (str): Dollar range (e.g., "1K–15K").
                disclosure_date (str): Published/filing date (YYYY-MM-DD).
                trade_date (str): Date the trade occurred (YYYY-MM-DD).
                disclosure_url (str): URL to the Capitol Trades detail page.
        """
        cutoff = datetime.now(timezone.utc) - timedelta(days=days_back)
        results = []
        page = 1

        while True:
            trade_ids = self._get_trade_ids_from_list_page(page)
            if not trade_ids:
                break

            # Fetch all 12 detail pages for this list-page batch in parallel
            batch = self._fetch_details_parallel(trade_ids)

            page_done = False
            for trade in batch:
                published = _parse_date(trade.get("disclosure_date", ""))
                if published and published < cutoff:
                    page_done = True
                else:
                    results.append(trade)

            if page_done:
                break

            page += 1
            if page > 50:
                logger.warning("Capitol Trades: hit page limit (50), stopping")
                break

        logger.info(f"Capitol Trades: scraped {len(results)} trades from last {days_back} days")
        return results


def _parse_detail_page(soup: BeautifulSoup, disclosure_url: str) -> Optional[dict]:
    """Parse all trade fields from a server-rendered trade detail page.

    The page title is the most reliable source for politician, action, ticker, and
    trade date. Body text provides amount, party, chamber, and published date.

    Title format: "{Politician} {sold|bought} {Company} ({TICKER}:XX) on YYYY-MM-DD"
    Body text format: "sell 1K–15K Republican / Senate / Arkansas {Politician} {TICKER}:XX
                       {Company} Traded YYYY-MM-DD Published YYYY-MM-DD ..."

    Args:
        soup: Parsed BeautifulSoup object for the trade detail page.
        disclosure_url: URL of this detail page.

    Returns:
        Trade dict with all fields, or None if ticker or action cannot be extracted.
    """
    title_tag = soup.find("title")
    title_text = title_tag.string.strip() if title_tag and title_tag.string else ""
    body_text = soup.get_text(" ", strip=True)

    # --- Ticker and trade date from page title ---
    ticker = None
    trade_date = ""
    politician = None
    action = None

    m = _TITLE_RE.match(title_text)
    if m:
        politician = m.group("politician").strip()
        verb = m.group("verb").lower()
        action = "buy" if verb in ("bought", "purchased") else "sell"
        ticker = m.group("ticker").upper()
        trade_date = m.group("trade_date")

    # --- Amount: "1K–15K", "15K–50K" ---
    am = re.search(r"(\d+[KMBkmb])\s*[–\-]\s*(\d+[KMBkmb])", body_text)
    amount_range = am.group(0) if am else "Unknown"

    # --- Party and chamber from "Republican / Senate / Arkansas" pattern ---
    pc_m = re.search(
        r"(Republican|Democrat|Independent)\s*/\s*(Senate|House)\s*/\s*\w+",
        body_text,
        re.IGNORECASE,
    )
    if pc_m:
        raw_party = pc_m.group(1).lower()
        party = "R" if raw_party == "republican" else ("D" if raw_party == "democrat" else "I")
        chamber = pc_m.group(2).capitalize()
    else:
        party = "Unknown"
        chamber = "Unknown"

    # --- Published (disclosure filing) date ---
    pub_m = re.search(r"Published\s+(\d{4}-\d{2}-\d{2})", body_text)
    disclosure_date = pub_m.group(1) if pub_m else ""

    # Fallback: use trade date as disclosure date if published not found
    if not disclosure_date:
        traded_m = re.search(r"Traded\s+(\d{4}-\d{2}-\d{2})", body_text)
        disclosure_date = traded_m.group(1) if traded_m else ""

    if not ticker or not action:
        # Expected for ETF/bond trades with no listed ticker (title shows "N/A")
        return None

    return {
        "ticker": ticker,
        "politician": politician or "Unknown",
        "party": party,
        "chamber": chamber,
        "action": action,
        "amount_range": amount_range,
        "disclosure_date": disclosure_date,
        "trade_date": trade_date,
        "disclosure_url": disclosure_url,
    }


def _parse_date(text: str) -> Optional[datetime]:
    """Parse a date string into a timezone-aware UTC datetime.

    Supports: YYYY-MM-DD, MM/DD/YYYY, D Mon YYYY (e.g., "17 Apr 2026").

    Args:
        text: Date string to parse.

    Returns:
        A UTC datetime, or None if empty or unparseable.
    """
    if not text:
        return None
    text = text.strip()
    for fmt in ("%Y-%m-%d", "%d %b %Y", "%m/%d/%Y"):
        try:
            return datetime.strptime(text, fmt).replace(tzinfo=timezone.utc)
        except ValueError:
            continue
    return None

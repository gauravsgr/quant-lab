"""Agent orchestrator: wires all data loaders, strategy, and execution into daily cycles.

Three entry points are provided:
    run_morning_cycle()    - 10:15 AM EST: collect signals and execute trades
    run_afternoon_cycle()  - 3:45 PM EST: mark-to-market, ghost trades, daily summary
    run_weekly_audit()     - Friday 4:00 PM EST: precision report, weekly Slack summary

The orchestrator is constructed once and passed to the APScheduler jobs in
agent/scheduler.py. All external API calls (Adanos, Capitol Trades, Alpaca News,
yfinance) go through the data loader modules and are never called directly.
"""
from datetime import datetime, timezone
from typing import Optional

from loguru import logger
from sqlalchemy.engine import Connection

import db.repository as repo
from brokers.base import Broker
from config.settings import Settings
from data.loaders.adanos import AdanosClient, BudgetExhausted
from data.loaders.alpaca_news import AlpacaNewsClient
from data.loaders.capitol_trades import CapitolTradesScraper
from data.loaders.yfinance_ratings import get_ratings_batch
from execution import portfolio, runner
from strategies.base import Signal
from strategies.confluence import ConfluenceStrategy, load_confluence_config
from utils.notifier import SlackNotifier


class Orchestrator:
    """Coordinates the three daily trading cycles.

    Constructs and holds references to all data loaders and the strategy.
    The broker, DB connection, and settings are injected at construction
    time to support testing without live API credentials.

    Attributes:
        _broker: Broker implementation for order submission and price data.
        _conn: Active SQLAlchemy connection shared across all DB calls.
        _settings: Application settings loaded from environment variables.
        _notifier: Optional SlackNotifier for trade alerts and reports.
        _strategy: ConfluenceStrategy instance loaded from YAML config.
        _adanos: Adanos API client with budget enforcement.
        _news: Alpaca News API client.
        _capitol: Capitol Trades scraper.
    """

    def __init__(
        self,
        broker: Broker,
        db_conn: Connection,
        settings: Settings,
        notifier: Optional[SlackNotifier] = None,
    ):
        """Initialize all data loaders and the strategy.

        Args:
            broker: A Broker implementation (Alpaca or BacktestBroker).
            db_conn: Active SQLAlchemy connection.
            settings: Application settings from load_settings().
            notifier: Optional SlackNotifier; Slack calls are skipped if None.
        """
        self._broker = broker
        self._conn = db_conn
        self._settings = settings
        self._notifier = notifier

        cfg = load_confluence_config()
        self._strategy = ConfluenceStrategy(cfg)
        self._adanos = AdanosClient(settings.adanos_api_key, db_conn)
        self._news = AlpacaNewsClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        self._capitol = CapitolTradesScraper()

    def run_morning_cycle(self) -> None:
        """Run the 10:15 AM EST morning cycle.

        Steps:
            1. Fetch buzzing tickers from Adanos (skips cycle if budget exhausted).
            2. Scrape Capitol Trades for disclosures in the last 3 days.
            3. Fetch Alpaca News headlines for buzzing tickers.
            4. Fetch yfinance analyst ratings for buzzing tickers.
            5. Score each ticker with ConfluenceStrategy.
            6. Submit orders for STRONG_BUY and STRONG_PUT signals above threshold.
        """
        logger.info("=== Morning cycle starting ===")

        try:
            buzzing = self._adanos.get_buzzing_tickers()
        except BudgetExhausted as e:
            logger.warning(f"Morning cycle skipped: {e}")
            return

        if not buzzing:
            logger.info("No buzzing tickers from Adanos today, morning cycle complete")
            return

        tickers = [b["ticker"] for b in buzzing]
        sentiment_map = {b["ticker"]: b["sentiment_score"] for b in buzzing}
        logger.info(f"Adanos: {len(tickers)} buzzing tickers: {tickers}")

        political_trades = self._capitol.get_recent_trades(days_back=3)
        pol_map: dict[str, dict] = {}
        for trade in political_trades:
            t = trade["ticker"]
            if t not in pol_map:
                pol_map[t] = trade  # keep first/latest disclosure per ticker

        news_map = self._news.get_news(tickers, limit=3)
        ratings_map = get_ratings_batch(tickers)

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        already_traded = repo.get_traded_tickers_for_date(self._conn, today)

        all_signals: list[Signal] = []
        for ticker in tickers:
            pol = pol_map.get(ticker)
            rating = ratings_map.get(ticker, {})
            news = news_map.get(ticker, [])
            headline = news[0]["headline"] if news else None

            signal = self._strategy.generate_signal(
                ticker=ticker,
                sentiment_score=sentiment_map.get(ticker),
                politician_action=pol.get("action") if pol else None,
                politician_name=pol.get("politician") if pol else None,
                politician_party=pol.get("party") if pol else None,
                politician_chamber=pol.get("chamber") if pol else None,
                politician_amount=pol.get("amount_range") if pol else None,
                disclosure_url=pol.get("disclosure_url") if pol else None,
                analyst_rating=rating.get("rating"),
                analyst_buy_count=rating.get("buy_count", 0),
                analyst_hold_count=rating.get("hold_count", 0),
                analyst_sell_count=rating.get("sell_count", 0),
                analyst_price_target=rating.get("price_target"),
                news_headline=headline,
            )
            all_signals.append(signal)
            logger.info(
                f"{ticker}: signal={signal.signal_type} confidence={signal.confidence:.2f} "
                f"sentiment={signal.sentiment_score} pol={signal.politician_action} "
                f"analyst={signal.analyst_rating}"
            )

        cfg = load_confluence_config()
        equity = self._broker.get_account_equity()
        traded_tickers: set[str] = set()

        for signal in all_signals:
            if signal.signal_type == "NEUTRAL":
                continue
            order_id = runner.submit_signal_order(
                signal=signal,
                broker=self._broker,
                db_conn=self._conn,
                account_equity=equity,
                min_confidence=cfg.min_confidence,
                require_approval=self._settings.require_approval,
                already_traded_tickers=already_traded | traded_tickers,
                notifier=self._notifier,
            )
            if order_id:
                traded_tickers.add(signal.ticker)

        logger.info(
            f"=== Morning cycle complete: {len(all_signals)} signals, {len(traded_tickers)} trades ==="
        )

    def run_afternoon_cycle(self) -> None:
        """Run the 3:45 PM EST afternoon cycle.

        Steps:
            1. Mark all open positions to market and update trailing stops.
            2. Record ghost trades for signals that fired but were not executed.
            3. Send daily Slack summary.
        """
        logger.info("=== Afternoon cycle starting ===")
        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")

        closed = portfolio.mark_to_market(self._broker, self._conn, date=today)

        signals_today = _load_signals_as_signal_objects(
            repo.get_signals_for_date(self._conn, today)
        )
        traded = repo.get_traded_tickers_for_date(self._conn, today)
        portfolio.record_ghost_trades(signals_today, traded, self._broker, self._conn, date=today)

        if self._notifier:
            ghost_rows = repo.get_ghost_trades_for_week(self._conn, since=today)
            self._notifier.send_daily_summary(closed, ghost_rows)

        logger.info(
            f"=== Afternoon cycle complete: {len(closed)} positions closed, "
            f"{len(signals_today) - len(traded)} ghost trades recorded ==="
        )

    def run_weekly_audit(self) -> None:
        """Run the Friday 4:00 PM EST weekly precision audit.

        Computes win-rate statistics by signal source type and sends a
        weekly Slack report with the best performer and ghost trade regrets.
        """
        logger.info("=== Weekly audit starting ===")
        from datetime import timedelta

        week_ago = (datetime.now(timezone.utc) - timedelta(days=7)).strftime("%Y-%m-%d")
        precision = repo.get_weekly_signal_precision(self._conn, since=week_ago)
        ghost_regrets = repo.get_ghost_trades_for_week(self._conn, since=week_ago)

        month = datetime.now(timezone.utc).strftime("%Y-%m")
        adanos_count = repo.get_adanos_call_count(self._conn, month)
        adanos_usage = {"call_count": adanos_count}

        open_orders = repo.get_open_orders(self._conn)
        best = None
        if open_orders:
            best = max(
                (o for o in open_orders if o.get("entry_price")),
                key=lambda o: (
                    (self._broker.get_latest_price(_extract_underlying(o["ticker"])) or o["entry_price"])
                    - o["entry_price"]
                ) * o["qty"],
                default=None,
            )
            if best:
                price = self._broker.get_latest_price(_extract_underlying(best["ticker"])) or best["entry_price"]
                best = {**best, "unrealized_pnl": (price - best["entry_price"]) * best["qty"]}

        if self._notifier:
            self._notifier.send_weekly_report(precision, adanos_usage, ghost_regrets, best)

        logger.info(f"=== Weekly audit complete: {len(precision)} signal sources analyzed ===")

    def dry_run(self) -> None:
        """Validate configuration and broker connectivity without making API calls.

        Checks that the broker authenticates, the DB path is set, and all
        settings are loaded. Does not call Adanos, Capitol Trades, or any
        external data source.
        """
        logger.info("Dry run: validating configuration...")
        equity = self._broker.get_account_equity()
        logger.info(f"  Broker connected. Account equity: ${equity:,.2f}")
        logger.info(f"  DB path: {self._settings.db_path}")
        logger.info(f"  Trading mode: {self._settings.trading_mode}")
        logger.info(f"  Require approval: {self._settings.require_approval}")
        logger.info(f"  Slack webhook configured: {'yes' if self._settings.slack_webhook_url else 'no'}")
        logger.info("Dry run complete. All systems ready.")


def _load_signals_as_signal_objects(rows: list[dict]) -> list[Signal]:
    """Convert DB signal rows back to Signal dataclass instances.

    Used in the afternoon cycle to reconstruct Signal objects from the
    morning's DB entries for ghost trade recording.

    Args:
        rows: List of signal row dicts from get_signals_for_date().

    Returns:
        List of Signal instances with the fields populated from the DB rows.
    """
    signals = []
    for row in rows:
        from strategies.base import Signal
        signals.append(Signal(
            ticker=row["ticker"],
            signal_type=row["signal_type"],
            confidence=row["confidence"],
            order_type=None,
            sentiment_score=row.get("sentiment_score"),
            politician_action=row.get("politician_action"),
            analyst_rating=row.get("analyst_rating"),
            news_headline=row.get("news_headline"),
        ))
    return signals


def _extract_underlying(ticker: str) -> str:
    """Extract the underlying stock symbol from an OCC option symbol.

    Args:
        ticker: Ticker string, which may be a plain symbol or OCC option symbol.

    Returns:
        The underlying stock symbol.
    """
    if len(ticker) > 6 and any(c.isdigit() for c in ticker):
        return "".join(c for c in ticker if c.isalpha()).rstrip("CP") or ticker
    return ticker

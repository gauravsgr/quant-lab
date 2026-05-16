"""Agent orchestrator: wires DataBundle, strategy registry, and execution into daily cycles.

Three entry points are provided:
    run_morning_cycle()    - 10:15 AM EST: build DataBundle, run all strategies, execute signals
    run_afternoon_cycle()  - 3:45 PM EST: mark-to-market, ghost trades, daily summary
    run_weekly_audit()     - Friday 4:00 PM EST: precision report, weekly Slack summary

The orchestrator is constructed once and passed to APScheduler jobs in agent/scheduler.py.

Morning cycle flow:
    1. Build DataBundle (cached bars/news, fresh political/buzz)
    2. Load enabled strategies from config/strategies/registry.yaml
    3. Run each strategy → list[Signal]
    4. Pass all signals to FullEnsembleStrategy
    5. Aggregate across strategies via SignalAggregator
    6. Submit orders for all non-conflicted signals above min_confidence
    7. Send rich multi-strategy Slack summary
"""
import importlib
from datetime import datetime, timezone
from typing import Optional

import yaml
from loguru import logger
from sqlalchemy.engine import Connection

import db.repository as repo
from agent.aggregator import SignalAggregator
from brokers.base import Broker
from config.settings import Settings
from data.bundle import DataBundle
from data.cache_manager import CacheManager
from data.loaders.adanos import AdanosClient, BudgetExhausted
from data.loaders.alpaca_news import AlpacaNewsClient
from data.loaders.capitol_trades import CapitolTradesScraper
from execution import portfolio, runner
from strategies.base import Signal, StandaloneStrategy
from strategies.ensembles.full_confluence import FullEnsembleStrategy
from utils.notifier import SlackNotifier

REGISTRY_PATH = "config/strategies/registry.yaml"


class Orchestrator:
    """Coordinates the three daily trading cycles.

    Attributes:
        _broker: Broker implementation for order submission and price data.
        _conn: Active SQLAlchemy connection shared across all DB calls.
        _settings: Application settings loaded from environment variables.
        _notifier: Optional SlackNotifier for trade alerts and reports.
        _adanos: Adanos API client with budget enforcement.
        _news: Alpaca News API client.
        _capitol: Capitol Trades scraper.
        _cache: Daily disk cache manager.
        _aggregator: Cross-strategy signal aggregator.
    """

    def __init__(
        self,
        broker: Broker,
        db_conn: Connection,
        settings: Settings,
        notifier: Optional[SlackNotifier] = None,
    ):
        self._broker = broker
        self._conn = db_conn
        self._settings = settings
        self._notifier = notifier
        self._adanos = AdanosClient(settings.adanos_api_key, db_conn)
        self._news = AlpacaNewsClient(settings.alpaca_api_key, settings.alpaca_secret_key)
        self._capitol = CapitolTradesScraper()
        self._cache = CacheManager()
        self._aggregator = SignalAggregator.from_registry()

    def run_morning_cycle(self) -> None:
        """Run the 10:15 AM EST morning cycle.

        Steps:
            1. Build DataBundle (cached bars/news, fresh political/buzz).
            2. Load enabled strategies from registry.
            3. Run each standalone strategy → list[Signal].
            4. Pass all signals to FullEnsembleStrategy.
            5. Aggregate via SignalAggregator (cross-strategy conviction scoring).
            6. Submit orders for signals above min_confidence threshold.
            7. Send rich multi-strategy Slack morning summary.
        """
        logger.info("=== Morning cycle starting ===")

        bundle = DataBundle.build(
            broker=self._broker,
            settings=self._settings,
            cache=self._cache,
            adanos=self._adanos,
            news_client=self._news,
            capitol=self._capitol,
        )

        if not bundle.watchlist:
            logger.error("Morning cycle aborted: empty watchlist")
            return

        strategies = _load_strategies(REGISTRY_PATH)
        if not strategies:
            logger.error("Morning cycle aborted: no strategies loaded from registry")
            return

        # Run each standalone strategy (excluding full_confluence — runs last)
        all_signals: list[Signal] = []
        signals_by_strategy: dict[str, list[Signal]] = {}

        for strategy in strategies:
            if strategy.name == "full_confluence":
                continue
            try:
                sigs = strategy.run(bundle)
                signals_by_strategy[strategy.name] = sigs
                all_signals.extend(sigs)
                logger.info(f"Strategy '{strategy.name}': {len(sigs)} signals")
            except Exception as e:
                logger.error(f"Strategy '{strategy.name}' raised an exception: {e}")
                signals_by_strategy[strategy.name] = []

        # Run full confluence ensemble with all prior signals as input
        ensemble = _find_ensemble(strategies)
        ensemble_signals: list[Signal] = []
        if ensemble is not None:
            try:
                ensemble.set_prior_signals(all_signals)
                ensemble_signals = ensemble.run(bundle)
                signals_by_strategy["full_confluence"] = ensemble_signals
                all_signals.extend(ensemble_signals)
                logger.info(f"FullEnsembleStrategy: {len(ensemble_signals)} ensemble signals")
            except Exception as e:
                logger.error(f"FullEnsembleStrategy raised an exception: {e}")

        # Aggregate cross-strategy signals
        aggregated = self._aggregator.aggregate(
            [s for s in all_signals if s.strategy_name != "full_confluence"]
        )

        today = datetime.now(timezone.utc).strftime("%Y-%m-%d")
        already_traded = repo.get_traded_tickers_for_date(self._conn, today)
        equity = self._broker.get_account_equity()
        cfg = _load_registry_cfg(REGISTRY_PATH)
        min_confidence = cfg.get("min_confidence_to_execute", 0.55)

        traded_tickers: set[str] = set()

        # --- Company info backfill for any signal tickers missing from bundle ---
        signal_tickers = {
            s.ticker
            for sigs in signals_by_strategy.values()
            for s in sigs
            if s.signal_type != "NEUTRAL"
        }
        missing_info = signal_tickers - set(bundle.company_info.keys())
        if missing_info:
            from data.bundle import _fetch_ratings_and_info
            logger.info(f"Backfilling company info for {len(missing_info)} signal tickers")
            _, new_info = _fetch_ratings_and_info(list(missing_info))
            bundle.company_info.update(new_info)
            for sigs in signals_by_strategy.values():
                for sig in sigs:
                    if sig.signal_type != "NEUTRAL" and sig.ticker in bundle.company_info:
                        if not sig.company_name or sig.company_name == sig.ticker:
                            sig.company_name = bundle.company_info[sig.ticker].get("name", sig.ticker)
                            sig.company_description = bundle.company_info[sig.ticker].get("description", "")

        # --- Pre-insert all non-neutral signals to get DB IDs for Slack buttons ---
        signal_id_map: dict[str, dict[str, int]] = {}
        for strategy_name, sigs in signals_by_strategy.items():
            for sig in sigs:
                if sig.signal_type == "NEUTRAL":
                    continue
                sid = repo.insert_signal(
                    self._conn,
                    ticker=sig.ticker,
                    signal_type=sig.signal_type,
                    sentiment_score=sig.sentiment_score,
                    politician_action=sig.politician_action,
                    politician_name=sig.politician_name,
                    politician_party=getattr(sig, "politician_party", None),
                    politician_chamber=getattr(sig, "politician_chamber", None),
                    politician_amount=getattr(sig, "politician_amount", None),
                    analyst_rating=sig.analyst_rating,
                    analyst_buy_count=sig.analyst_buy_count,
                    analyst_hold_count=sig.analyst_hold_count,
                    analyst_sell_count=sig.analyst_sell_count,
                    analyst_price_target=sig.analyst_price_target,
                    news_headline=sig.news_headline,
                    confidence=sig.confidence,
                    technical_score=getattr(sig, "technical_score", None),
                    technical_rsi=getattr(sig, "technical_rsi", None),
                    technical_direction=getattr(sig, "technical_direction", None),
                    strategy_name=getattr(sig, "strategy_name", None),
                )
                signal_id_map.setdefault(sig.ticker, {})[strategy_name] = sid

        # --- Fetch ATM options context for all actionable signals ---
        from brokers.alpaca import AlpacaBroker
        options_ctx: dict[tuple, dict] = {}
        if isinstance(self._broker, AlpacaBroker):
            for sigs in signals_by_strategy.values():
                for sig in sigs:
                    if sig.signal_type == "NEUTRAL":
                        continue
                    opt_type = "call" if sig.signal_type == "STRONG_BUY" else "put"
                    key = (sig.ticker, opt_type)
                    if key not in options_ctx:
                        try:
                            ctx = self._broker.find_atm_contract_full(sig.ticker, opt_type)
                            if ctx:
                                options_ctx[key] = ctx
                        except Exception:
                            pass

        # --- Execution (skipped entirely when require_approval=True; buttons handle it) ---
        if not self._settings.require_approval:
            for ag in aggregated:
                if ag.conflict or ag.final_signal_type == "NEUTRAL":
                    continue
                if ag.final_confidence < min_confidence:
                    continue
                sig = ag.best_signal
                pre_sid = signal_id_map.get(sig.ticker, {}).get(sig.strategy_name)
                order_id = runner.submit_signal_order(
                    signal=sig,
                    broker=self._broker,
                    db_conn=self._conn,
                    account_equity=equity,
                    min_confidence=min_confidence,
                    require_approval=False,
                    already_traded_tickers=already_traded | traded_tickers,
                    notifier=self._notifier,
                    signal_id=pre_sid,
                )
                if order_id:
                    traded_tickers.add(sig.ticker)

            for sig in ensemble_signals:
                if sig.signal_type == "NEUTRAL":
                    continue
                if sig.confidence < min_confidence:
                    continue
                if sig.ticker in (already_traded | traded_tickers):
                    continue
                pre_sid = signal_id_map.get(sig.ticker, {}).get(sig.strategy_name)
                order_id = runner.submit_signal_order(
                    signal=sig,
                    broker=self._broker,
                    db_conn=self._conn,
                    account_equity=equity,
                    min_confidence=min_confidence,
                    require_approval=False,
                    already_traded_tickers=already_traded | traded_tickers,
                    notifier=self._notifier,
                    signal_id=pre_sid,
                )
                if order_id:
                    traded_tickers.add(sig.ticker)

        if self._notifier:
            self._notifier.send_morning_summary_multi_strategy(
                signals_by_strategy=signals_by_strategy,
                aggregated=aggregated,
                traded_count=len(traded_tickers),
                tickers_scanned=len(bundle.watchlist),
                pol_count=len(bundle.political_trades),
                catalyst_count=len(bundle.catalyst_hits),
                signal_id_map=signal_id_map,
                options_ctx=options_ctx,
                require_approval=self._settings.require_approval,
            )

        logger.info(
            f"=== Morning cycle complete: {len(traded_tickers)} trades, "
            f"{len(all_signals)} signals across {len(strategies)} strategies ==="
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
        """Run the Friday 4:00 PM EST weekly precision audit."""
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
        """Validate configuration and broker connectivity without making API calls."""
        logger.info("Dry run: validating configuration...")
        equity = self._broker.get_account_equity()
        logger.info(f"  Broker connected. Account equity: ${equity:,.2f}")
        logger.info(f"  DB path: {self._settings.db_path}")
        logger.info(f"  Trading mode: {self._settings.trading_mode}")
        logger.info(f"  Require approval: {self._settings.require_approval}")
        logger.info(f"  Slack bot token configured: {'yes' if self._settings.slack_bot_token else 'no'}")
        logger.info(f"  Slack interactive buttons: {'enabled' if self._settings.slack_app_token else 'disabled (SLACK_APP_TOKEN not set)'}")

        strategies = _load_strategies(REGISTRY_PATH)
        logger.info(f"  Strategies loaded: {[s.name for s in strategies]}")
        logger.info("Dry run complete. All systems ready.")


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _load_registry_cfg(path: str = REGISTRY_PATH) -> dict:
    try:
        with open(path) as f:
            return yaml.safe_load(f) or {}
    except Exception as e:
        logger.warning(f"Could not load registry config: {e}")
        return {}


def _load_strategies(registry_path: str) -> list[StandaloneStrategy]:
    """Instantiate all enabled strategies from the registry YAML."""
    cfg = _load_registry_cfg(registry_path)
    entries = cfg.get("strategies", [])
    strategies: list[StandaloneStrategy] = []

    for entry in entries:
        if not entry.get("enabled", True):
            logger.debug(f"Strategy '{entry.get('name')}' is disabled, skipping")
            continue
        class_path = entry.get("class", "")
        if not class_path:
            continue
        try:
            module_path, class_name = class_path.rsplit(".", 1)
            module = importlib.import_module(module_path)
            cls = getattr(module, class_name)
            strategies.append(cls())
            logger.debug(f"Loaded strategy '{entry['name']}' from {class_path}")
        except Exception as e:
            logger.error(f"Failed to load strategy '{entry.get('name')}': {e}")

    return strategies


def _find_ensemble(strategies: list[StandaloneStrategy]) -> Optional[FullEnsembleStrategy]:
    """Return the FullEnsembleStrategy from the strategy list, or None."""
    for s in strategies:
        if isinstance(s, FullEnsembleStrategy):
            return s
    return None


def _load_signals_as_signal_objects(rows: list[dict]) -> list[Signal]:
    """Convert DB signal rows back to Signal dataclass instances for the afternoon cycle."""
    signals = []
    for row in rows:
        signals.append(Signal(
            ticker=row["ticker"],
            signal_type=row["signal_type"],
            confidence=row["confidence"],
            order_type=None,
            sentiment_score=row.get("sentiment_score"),
            politician_action=row.get("politician_action"),
            analyst_rating=row.get("analyst_rating"),
            news_headline=row.get("news_headline"),
            strategy_name=row.get("strategy_name") or "",
        ))
    return signals


def _extract_underlying(ticker: str) -> str:
    """Extract the underlying stock symbol from an OCC option symbol."""
    if len(ticker) > 6 and any(c.isdigit() for c in ticker):
        return "".join(c for c in ticker if c.isalpha()).rstrip("CP") or ticker
    return ticker

#!/usr/bin/env python3
"""CLI entry point for the quant-lab trading system.

Subcommands:
    agent       Start the alternative data trading agent (scheduled or one-shot).
    trade       Run a price-bar strategy in live or paper mode (placeholder).
    backtest    Replay a strategy over historical yfinance data.

Usage:
    python main.py agent
    python main.py agent --dry-run
    python main.py agent --run-now morning
    python main.py agent --run-now afternoon
    python main.py agent --run-now weekly
    python main.py backtest --strategy rsi_mean_revert --symbol AAPL --start 2023-01-01 --end 2024-01-01
    python main.py trade --strategy rsi_mean_revert --broker alpaca --mode paper
"""
import argparse
import sys

from dotenv import load_dotenv
from loguru import logger

load_dotenv()


def _build_alpaca_broker(settings):
    """Construct an AlpacaBroker from settings.

    Args:
        settings: A Settings instance loaded from environment variables.

    Returns:
        An initialized AlpacaBroker.
    """
    from brokers.alpaca import AlpacaBroker
    return AlpacaBroker(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        base_url=settings.alpaca_base_url,
        mode=settings.trading_mode,
    )


def cmd_agent(args) -> None:
    """Handle the 'agent' subcommand.

    Supports three modes:
        --dry-run: validate config and connectivity without any side effects.
        --run-now CYCLE: execute one named cycle immediately and exit.
        (no flag): start the APScheduler and block indefinitely.

    Args:
        args: Parsed argparse namespace with dry_run and run_now attributes.
    """
    from config.settings import load_settings
    from db.models import create_all, engine
    from utils.logging import configure_logger
    from utils.notifier import SlackNotifier
    from agent.orchestrator import Orchestrator
    from agent import scheduler as sched_module

    configure_logger()
    settings = load_settings()

    create_all()

    with engine.connect() as conn:
        broker = _build_alpaca_broker(settings)
        notifier = SlackNotifier(settings.slack_bot_token, settings.slack_channel_id)
        orchestrator = Orchestrator(broker, conn, settings, notifier)

        if args.dry_run:
            orchestrator.dry_run()
            return

        if args.run_now:
            cycle = args.run_now.lower()
            if cycle == "morning":
                orchestrator.run_morning_cycle()
            elif cycle == "afternoon":
                orchestrator.run_afternoon_cycle()
            elif cycle == "weekly":
                orchestrator.run_weekly_audit()
            else:
                logger.error(f"Unknown cycle: {cycle}. Use morning, afternoon, or weekly.")
                sys.exit(1)
            return

        from utils.slack_actions import start_socket_mode
        start_socket_mode(broker, settings, notifier)
        sched_module.start(orchestrator, timezone=settings.agent_timezone)


def cmd_trade(args) -> None:
    """Handle the 'trade' subcommand (placeholder for price-bar strategies).

    Args:
        args: Parsed argparse namespace with strategy, broker, and mode attributes.
    """
    from config.settings import load_settings
    from db.models import create_all
    from utils.logging import configure_logger

    configure_logger()
    settings = load_settings()
    create_all()

    logger.warning(
        "'trade' subcommand is for price-bar strategies; use 'agent' for the alt-data agent."
    )
    # Placeholder: wire a Strategy + Broker into a run loop here for future RSI/price strategies.


def cmd_backtest(args) -> None:
    """Handle the 'backtest' subcommand.

    Downloads historical data via yfinance, normalizes the DataFrame, and
    runs the backtesting engine with a dummy strategy signal function.

    Args:
        args: Parsed argparse namespace with strategy, symbol, start, and end attributes.
    """
    from config.settings import load_settings
    from utils.logging import configure_logger
    from backtesting.engine import run_backtest
    import yfinance as yf
    import pandas as pd

    configure_logger()
    settings = load_settings()

    logger.info(f"Backtesting placeholder: {args.strategy} on {args.symbol} from {args.start} to {args.end}")

    df = yf.download(args.symbol, start=args.start, end=args.end, auto_adjust=True)
    if df.empty:
        logger.error(f"No data returned for {args.symbol}")
        sys.exit(1)

    df = df.reset_index()
    df.columns = [c.lower() if isinstance(c, str) else c[0].lower() for c in df.columns]
    df = df.rename(columns={"date": "timestamp"})
    df["timestamp"] = df["timestamp"].astype(str)

    bars = {args.symbol: df}

    def dummy_strategy(ticker, bar):
        return None  # Replace with a real strategy function.

    results = run_backtest(dummy_strategy, [args.symbol], bars)
    logger.info(
        f"Backtest complete: {results['trade_count']} trades | "
        f"total return ${results['total_return']:+,.2f} | "
        f"max drawdown {results['max_drawdown']:.1%} | "
        f"Sharpe {results['sharpe_ratio']:.2f}"
    )


def main() -> None:
    """Parse CLI arguments and dispatch to the appropriate command handler."""
    parser = argparse.ArgumentParser(
        prog="quant-lab",
        description="Algorithmic trading research and execution framework",
    )
    sub = parser.add_subparsers(dest="command", required=True)

    agent_p = sub.add_parser("agent", help="Run the alternative data trading agent")
    agent_p.add_argument("--dry-run", action="store_true",
                         help="Validate config and connectivity without executing trades")
    agent_p.add_argument("--run-now", metavar="CYCLE",
                         help="Run a single cycle immediately (morning|afternoon|weekly)")

    trade_p = sub.add_parser("trade", help="Run a price-bar strategy in live/paper mode")
    trade_p.add_argument("--strategy", required=True)
    trade_p.add_argument("--broker", default="alpaca")
    trade_p.add_argument("--mode", default="paper")

    bt_p = sub.add_parser("backtest", help="Backtest a strategy on historical data")
    bt_p.add_argument("--strategy", required=True)
    bt_p.add_argument("--symbol", required=True)
    bt_p.add_argument("--start", required=True)
    bt_p.add_argument("--end", required=True)

    args = parser.parse_args()

    if args.command == "agent":
        cmd_agent(args)
    elif args.command == "trade":
        cmd_trade(args)
    elif args.command == "backtest":
        cmd_backtest(args)


if __name__ == "__main__":
    main()

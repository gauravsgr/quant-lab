"""Historical replay engine for strategy backtesting.

Replays a strategy over multi-ticker OHLCV bar data using the in-memory
BacktestBroker. Positions are sized at 5% of equity per signal. Results are
written to backtesting/results/ as timestamped JSON files.

Typical usage:
    def my_strategy(ticker, bar):
        return {"signal_type": "STRONG_BUY"} if bar["close"] > threshold else None

    results = run_backtest(my_strategy, ["AAPL"], bars_dict)
"""
import json
import os
from datetime import datetime, timezone
from typing import Callable, Optional

import pandas as pd
from loguru import logger

from brokers.backtest_broker import BacktestBroker
from backtesting.metrics import win_rate, total_return, max_drawdown, sharpe_ratio


def run_backtest(
    strategy_fn: Callable[[str, dict], Optional[dict]],
    tickers: list[str],
    bars: dict[str, pd.DataFrame],
    initial_equity: float = 100_000.0,
    output_dir: str = "backtesting/results",
) -> dict:
    """Replay a strategy over historical OHLCV bars and compute performance metrics.

    Iterates over all timestamps common to the provided bar DataFrames. At each
    timestamp, the strategy function is called for each ticker. STRONG_BUY signals
    result in a buy order sized at 5% of current equity; STRONG_PUT results in a sell.

    Args:
        strategy_fn: Callable with signature (ticker: str, bar: dict) -> Optional[dict].
            Should return a dict with at least {"signal_type": "STRONG_BUY"|"STRONG_PUT"|"NEUTRAL"}
            or None to skip.
        tickers: List of ticker symbols to test.
        bars: Dict mapping each ticker to a DataFrame with columns:
            timestamp, open, high, low, close, volume.
        initial_equity: Starting account value in USD (default 100,000).
        output_dir: Directory to write the results JSON file.

    Returns:
        Dict with keys:
            initial_equity (float): Starting capital.
            final_equity (float): Ending capital.
            total_return (float): Sum of all P&L in USD.
            win_rate (float): Percentage of winning trades.
            max_drawdown (float): Peak-to-trough drawdown as a fraction.
            sharpe_ratio (float): Annualized Sharpe ratio (252 trading days).
            trade_count (int): Number of trades executed.
            trades (list): Full trade log with per-trade details.
            equity_curve (list): Account equity at each timestamp.
            generated_at (str): ISO 8601 UTC timestamp of when the backtest ran.
    """
    broker = BacktestBroker(initial_equity)
    trade_log = []
    equity_curve = [initial_equity]
    pnls = []

    # Align all tickers on their common set of timestamps.
    all_timestamps = sorted(set(
        ts for df in bars.values() for ts in df["timestamp"].tolist()
    ))

    for ts in all_timestamps:
        for ticker in tickers:
            df = bars.get(ticker)
            if df is None:
                continue
            bar = df[df["timestamp"] == ts]
            if bar.empty:
                continue

            row = bar.iloc[0].to_dict()
            price = row.get("close", 0)
            broker.set_price(ticker, price)

            signal = strategy_fn(ticker, row)
            if not signal:
                continue

            signal_type = signal.get("signal_type", "NEUTRAL")
            if signal_type == "NEUTRAL":
                continue

            side = "buy" if signal_type == "STRONG_BUY" else "sell"
            qty = round((broker.get_account_equity() * 0.05) / price, 4) if price > 0 else 0
            if qty <= 0:
                continue

            result = broker.submit_order(ticker, qty, side)
            entry_price = result.filled_price or price

            trade_log.append({
                "timestamp": str(ts),
                "ticker": ticker,
                "signal_type": signal_type,
                "side": side,
                "qty": qty,
                "entry_price": entry_price,
                "pnl": None,
            })
            logger.debug(f"Backtest trade: {side} {qty} {ticker} @ {entry_price}")

        equity_curve.append(broker.get_account_equity())

    # Compute P&L for positions still open at end of the backtest period.
    for pos in broker.get_positions():
        last_price = broker.get_latest_price(pos["ticker"]) or pos["avg_entry_price"]
        pnl = (last_price - pos["avg_entry_price"]) * pos["qty"]
        pnls.append(pnl)
        for trade in reversed(trade_log):
            if trade["ticker"] == pos["ticker"] and trade["pnl"] is None:
                trade["pnl"] = pnl
                break

    results = {
        "initial_equity": initial_equity,
        "final_equity": broker.get_account_equity(),
        "total_return": total_return(pnls),
        "win_rate": win_rate(pnls),
        "max_drawdown": max_drawdown(equity_curve),
        "sharpe_ratio": sharpe_ratio(pnls),
        "trade_count": len(trade_log),
        "trades": trade_log,
        "equity_curve": equity_curve,
        "generated_at": datetime.now(timezone.utc).isoformat(),
    }

    _write_results(results, output_dir)
    return results


def _write_results(results: dict, output_dir: str) -> None:
    """Write backtest results to a timestamped JSON file.

    Args:
        results: Results dict from run_backtest().
        output_dir: Directory path; created if it does not exist.
    """
    os.makedirs(output_dir, exist_ok=True)
    ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    path = os.path.join(output_dir, f"backtest_{ts}.json")
    with open(path, "w") as f:
        json.dump(results, f, indent=2, default=str)
    logger.info(f"Backtest results written to {path}")

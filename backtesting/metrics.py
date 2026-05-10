"""Performance metrics for backtesting and weekly precision audits.

All functions accept a list of P&L values (floats in USD) or an equity curve
(list of account value snapshots) and return a single scalar metric.

Typical usage:
    pnls = [150.0, -80.0, 220.0, -40.0]
    wr = win_rate(pnls)          # percentage of positive trades
    tr = total_return(pnls)      # sum of all P&L
    mdd = max_drawdown(equity)   # peak-to-trough fraction
    sr = sharpe_ratio(pnls)      # annualized Sharpe (252 days)
"""
import math
from typing import Optional


def win_rate(pnls: list[float]) -> float:
    """Compute the win rate as a percentage.

    Args:
        pnls: List of per-trade profit and loss values in USD.

    Returns:
        Percentage of trades with positive P&L, rounded to 2 decimal places.
        Returns 0.0 for empty input.
    """
    if not pnls:
        return 0.0
    wins = sum(1 for p in pnls if p > 0)
    return round(wins / len(pnls) * 100, 2)


def total_return(pnls: list[float]) -> float:
    """Compute the total realized return across all trades.

    Args:
        pnls: List of per-trade profit and loss values in USD.

    Returns:
        Sum of all P&L values in USD, rounded to 4 decimal places.
    """
    return round(sum(pnls), 4)


def max_drawdown(equity_curve: list[float]) -> float:
    """Compute the maximum peak-to-trough drawdown over the equity curve.

    Args:
        equity_curve: List of account equity snapshots in chronological order.

    Returns:
        Maximum drawdown as a fraction in [0.0, 1.0] (e.g., 0.15 means 15%),
        rounded to 4 decimal places. Returns 0.0 for empty input.
    """
    if not equity_curve:
        return 0.0
    peak = equity_curve[0]
    max_dd = 0.0
    for val in equity_curve:
        peak = max(peak, val)
        dd = (peak - val) / peak if peak > 0 else 0.0
        max_dd = max(max_dd, dd)
    return round(max_dd, 4)


def sharpe_ratio(pnls: list[float], risk_free_rate: float = 0.0) -> float:
    """Compute the annualized Sharpe ratio assuming daily P&L returns.

    Annualizes by scaling the mean/std ratio by sqrt(252) trading days.

    Args:
        pnls: List of per-trade P&L values in USD.
        risk_free_rate: Daily risk-free rate in USD (default 0.0).

    Returns:
        Annualized Sharpe ratio, rounded to 4 decimal places.
        Returns 0.0 if there are fewer than 2 trades or standard deviation is zero.
    """
    if len(pnls) < 2:
        return 0.0
    n = len(pnls)
    mean = sum(pnls) / n
    variance = sum((p - mean) ** 2 for p in pnls) / (n - 1)
    std = math.sqrt(variance)
    if std == 0:
        return 0.0
    return round((mean - risk_free_rate) / std * math.sqrt(252), 4)


def precision_by_source(results: list[dict]) -> dict:
    """Compute win rate grouped by signal type for a list of trade results.

    Args:
        results: List of dicts, each with keys:
            signal_type (str): The signal classification (e.g., "STRONG_BUY").
            pnl (float): Trade P&L in USD.

    Returns:
        Dict mapping each signal_type to a stats dict with keys:
            count (int): Number of trades with that signal type.
            win_rate (float): Percentage of winning trades.
            total_pnl (float): Sum of P&L for that signal type.
    """
    grouped: dict[str, list[float]] = {}
    for r in results:
        key = r.get("signal_type", "UNKNOWN")
        grouped.setdefault(key, []).append(r.get("pnl", 0.0))

    return {
        src: {
            "count": len(pnls),
            "win_rate": win_rate(pnls),
            "total_pnl": total_return(pnls),
        }
        for src, pnls in grouped.items()
    }

"""Technical indicator computation from OHLCV bar data.

Computes RSI, MACD histogram, and moving average trend signals from a list of
bar dicts returned by Broker.get_bars(). Requires at least 26 bars for MACD;
returns a neutral result with None values if insufficient data is available.

Typical usage:
    bars = broker.get_bars(ticker, limit=60)
    signal = compute_technical_signal(bars)
    # signal = {"score": 0.67, "direction": "bullish", "rsi": 42.1, ...}
"""
from typing import Optional

import pandas as pd
from loguru import logger


def compute_technical_signal(bars: list[dict]) -> dict:
    """Compute RSI, MACD, and 20/50-day MA signals from OHLCV bar data.

    Each indicator votes +1 (bullish) or -1 (bearish). The composite score
    is the mean of all votes, producing a value in [-1, 1]. With 50+ bars,
    the 20-day vs 50-day MA crossover adds a fourth vote.

    Indicator rules:
        RSI(14) < 35  → bullish (+1); RSI > 65 → bearish (-1)
        MACD histogram > 0 → bullish (+1); < 0 → bearish (-1)
        Price > 20-day MA → bullish (+1); below → bearish (-1)
        20-day MA > 50-day MA → bullish (+1); below → bearish (-1)  [50+ bars]

    Args:
        bars: List of bar dicts with at least a "close" key. Returned by
            Broker.get_bars(ticker, limit=60).

    Returns:
        Dict with keys:
            score (float): Composite in [-1.0, 1.0]. Positive = bullish.
            direction (str): "bullish", "bearish", or "neutral".
            rsi (float | None): Last RSI value; None if insufficient data.
            macd (str): "bullish", "bearish", or "neutral".
            ma (str): "above" (price > 20MA) or "below".
    """
    if len(bars) < 26:
        return {"score": 0.0, "direction": "neutral", "rsi": None, "macd": "neutral", "ma": "neutral"}

    try:
        from ta.momentum import RSIIndicator
        from ta.trend import MACD, SMAIndicator

        close = pd.to_numeric(pd.DataFrame(bars)["close"])

        rsi_val = RSIIndicator(close, window=14).rsi().iloc[-1]
        rsi_score = 1 if rsi_val < 35 else (-1 if rsi_val > 65 else 0)

        hist = MACD(close).macd_diff().iloc[-1]
        macd_score = 1 if hist > 0 else (-1 if hist < 0 else 0)

        price = close.iloc[-1]
        ma20 = SMAIndicator(close, window=20).sma_indicator().iloc[-1]
        ma20_score = 1 if price > ma20 else -1

        scores = [rsi_score, macd_score, ma20_score]

        if len(bars) >= 50:
            ma50 = SMAIndicator(close, window=50).sma_indicator().iloc[-1]
            scores.append(1 if ma20 > ma50 else -1)

        composite = sum(scores) / len(scores)
        direction = "bullish" if composite > 0 else ("bearish" if composite < 0 else "neutral")

        return {
            "score": round(composite, 3),
            "direction": direction,
            "rsi": round(rsi_val, 1),
            "macd": "bullish" if macd_score > 0 else ("bearish" if macd_score < 0 else "neutral"),
            "ma": "above" if ma20_score > 0 else "below",
        }
    except Exception as e:
        logger.warning(f"Technical indicator computation failed: {e}")
        return {"score": 0.0, "direction": "neutral", "rsi": None, "macd": "neutral", "ma": "neutral"}

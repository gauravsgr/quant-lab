"""Pre-trade validation checks for signal-to-order conversion.

Validates each signal against a checklist before any broker interaction
occurs. All checks raise ValidationError on failure so that runner.py
can handle the error in one place.

Checks performed:
    1. Signal type must be STRONG_BUY or STRONG_PUT (not NEUTRAL).
    2. Confidence must meet the minimum threshold.
    3. Ticker must not have been traded today already.
"""
from typing import Optional

from loguru import logger

from strategies.base import Signal


class ValidationError(Exception):
    """Raised when a signal fails one or more pre-trade checks."""


def pre_trade_check(
    signal: Signal,
    min_confidence: float = 0.65,
    already_traded_tickers: Optional[set] = None,
) -> None:
    """Run the full pre-trade checklist. Raises ValidationError if any check fails.

    Args:
        signal: The Signal instance to validate.
        min_confidence: Minimum confidence required to proceed (default 0.65).
        already_traded_tickers: Set of ticker strings already traded today.
            If the signal's ticker is in this set, it is rejected.

    Raises:
        ValidationError: If the signal fails any check. The error message
            describes which check failed.
    """
    if signal.signal_type == "NEUTRAL":
        raise ValidationError(f"{signal.ticker}: NEUTRAL signal, skipping")

    if signal.confidence < min_confidence:
        raise ValidationError(
            f"{signal.ticker}: confidence {signal.confidence:.2f} below minimum {min_confidence}"
        )

    if already_traded_tickers and signal.ticker in already_traded_tickers:
        raise ValidationError(f"{signal.ticker}: already traded today, skipping duplicate")

    logger.debug(f"Pre-trade checks passed for {signal.ticker} ({signal.signal_type})")

"""Sliding-window rate limiter for external API calls.

Provides a decorator factory that throttles a function to at most
`calls_per_min` invocations within any 60-second window. The limiter
is thread-safe and shared across all calls to the same decorated function.

Typical usage:
    @rate_limit(calls_per_min=200)
    def fetch_news(tickers):
        ...
"""
import time
import threading
from collections import deque
from functools import wraps
from typing import Callable


def rate_limit(calls_per_min: int) -> Callable:
    """Return a decorator that enforces a per-minute call rate limit.

    Uses a sliding 60-second window with a shared timestamp deque. When the
    window is full, the decorator sleeps until the oldest call expires.

    The lock is held during the sleep to prevent burst scenarios where
    multiple threads wake up simultaneously and all proceed past the check.

    Args:
        calls_per_min: Maximum number of calls permitted in any 60-second window.

    Returns:
        A decorator that wraps a function with rate-limiting behaviour.

    Example:
        @rate_limit(calls_per_min=60)
        def my_api_call():
            ...
    """
    lock = threading.Lock()
    timestamps: deque = deque()

    def decorator(fn: Callable) -> Callable:
        @wraps(fn)
        def wrapper(*args, **kwargs):
            with lock:
                now = time.monotonic()
                window_start = now - 60.0

                # Drop timestamps that have exited the 60-second window.
                while timestamps and timestamps[0] <= window_start:
                    timestamps.popleft()

                if len(timestamps) >= calls_per_min:
                    # Sleep until the oldest call in the window falls out.
                    sleep_for = timestamps[0] - window_start
                    time.sleep(sleep_for)
                    now = time.monotonic()

                timestamps.append(now)

            return fn(*args, **kwargs)
        return wrapper
    return decorator

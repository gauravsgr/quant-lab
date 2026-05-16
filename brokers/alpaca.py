"""Alpaca broker implementation for paper and live trading.

Uses the alpaca-py SDK to submit equity and options orders, fetch market data,
and query account state. The trading mode (paper vs. live) is determined by the
TRADING_MODE environment variable and passed to TradingClient at construction.

Typical usage:
    broker = AlpacaBroker(
        api_key=settings.alpaca_api_key,
        secret_key=settings.alpaca_secret_key,
        base_url=settings.alpaca_base_url,
        mode=settings.trading_mode,   # "paper" or "live"
    )
    equity = broker.get_account_equity()
"""
import os
from datetime import date as date_type, datetime, timedelta, timezone
from typing import Optional

from alpaca.trading.client import TradingClient
from alpaca.trading.requests import (
    MarketOrderRequest, LimitOrderRequest, TrailingStopOrderRequest,
    GetAssetsRequest,
)
from alpaca.trading.enums import OrderSide, TimeInForce, AssetClass, OrderType as AlpacaOrderType
from alpaca.data.historical import StockHistoricalDataClient, OptionHistoricalDataClient
from alpaca.data.requests import StockBarsRequest, OptionChainRequest
from alpaca.data.timeframe import TimeFrame
from loguru import logger

from brokers.base import Broker, OrderResult


class AlpacaBroker(Broker):
    """Alpaca paper and live trading broker.

    Wraps three Alpaca SDK clients:
        TradingClient              - order submission and account queries
        StockHistoricalDataClient  - stock bars and news
        OptionHistoricalDataClient - options chain data

    Attributes:
        _trading: Alpaca TradingClient instance.
        _data: Alpaca StockHistoricalDataClient instance.
        _options_data: Alpaca OptionHistoricalDataClient instance.
        _mode: "paper" or "live".
    """

    def __init__(self, api_key: str, secret_key: str, base_url: str, mode: str = "paper"):
        """Initialize the broker and authenticate with Alpaca.

        Args:
            api_key: Alpaca API key ID.
            secret_key: Alpaca API secret key.
            base_url: Alpaca REST base URL (not used directly; TradingClient
                determines endpoint from the paper flag).
            mode: "paper" for the paper trading account, "live" for real money.
        """
        paper = mode.lower() == "paper"
        self._trading = TradingClient(api_key, secret_key, paper=paper)
        self._data = StockHistoricalDataClient(api_key, secret_key)
        self._options_data = OptionHistoricalDataClient(api_key, secret_key)
        self._mode = mode
        logger.info(f"AlpacaBroker initialized in {mode} mode")

    def submit_order(
        self,
        ticker: str,
        qty: float,
        side: str,
        order_type: str = "market",
        time_in_force: str = "day",
        limit_price: Optional[float] = None,
        trail_percent: Optional[float] = None,
    ) -> OrderResult:
        """Submit an equity order to Alpaca.

        Args:
            ticker: Stock symbol.
            qty: Number of shares.
            side: "buy" or "sell".
            order_type: "market", "limit", or "trailing_stop".
            time_in_force: "day" or "gtc".
            limit_price: Required for limit orders.
            trail_percent: Required for trailing stop orders.

        Returns:
            An OrderResult with the broker order ID and fill details.
        """
        alpaca_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL
        tif = TimeInForce.DAY if time_in_force.lower() == "day" else TimeInForce.GTC

        if trail_percent:
            req = TrailingStopOrderRequest(
                symbol=ticker,
                qty=qty,
                side=alpaca_side,
                time_in_force=tif,
                trail_percent=trail_percent,
            )
        elif order_type == "limit" and limit_price:
            req = LimitOrderRequest(
                symbol=ticker,
                qty=qty,
                side=alpaca_side,
                time_in_force=tif,
                limit_price=limit_price,
            )
        else:
            req = MarketOrderRequest(
                symbol=ticker,
                qty=qty,
                side=alpaca_side,
                time_in_force=tif,
            )

        order = self._trading.submit_order(req)
        filled_price = float(order.filled_avg_price) if order.filled_avg_price else None

        logger.info(f"Order submitted: {side.upper()} {qty} {ticker} @ {filled_price} [{order.id}]")
        return OrderResult(
            broker_order_id=str(order.id),
            ticker=ticker,
            qty=float(qty),
            filled_price=filled_price,
            status=str(order.status),
            order_type=order_type,
        )

    def submit_options_order(
        self,
        symbol: str,
        qty: int,
        side: str,
        order_type: str = "market",
        limit_price: Optional[float] = None,
    ) -> OrderResult:
        """Submit an options order using an OCC contract symbol.

        Args:
            symbol: OCC option symbol (e.g., "AAPL230616C00150000").
            qty: Number of contracts.
            side: "buy" or "sell".
            order_type: "market" or "limit".
            limit_price: Required for limit orders.

        Returns:
            An OrderResult with the broker order ID and fill details.
        """
        alpaca_side = OrderSide.BUY if side.lower() == "buy" else OrderSide.SELL

        if order_type == "limit" and limit_price:
            req = LimitOrderRequest(
                symbol=symbol,
                qty=qty,
                side=alpaca_side,
                time_in_force=TimeInForce.DAY,
                limit_price=limit_price,
            )
        else:
            req = MarketOrderRequest(
                symbol=symbol,
                qty=qty,
                side=alpaca_side,
                time_in_force=TimeInForce.DAY,
            )

        order = self._trading.submit_order(req)
        filled_price = float(order.filled_avg_price) if order.filled_avg_price else None

        logger.info(f"Options order submitted: {side.upper()} {qty}x {symbol} @ {filled_price} [{order.id}]")
        return OrderResult(
            broker_order_id=str(order.id),
            ticker=symbol,
            qty=float(qty),
            filled_price=filled_price,
            status=str(order.status),
            order_type=order_type,
        )

    def get_positions(self) -> list[dict]:
        """Return all current Alpaca positions as a list of normalized dicts.

        Returns:
            List of dicts with keys: ticker, qty, market_value,
            unrealized_pl, avg_entry_price, current_price.
        """
        positions = self._trading.get_all_positions()
        return [
            {
                "ticker": p.symbol,
                "qty": float(p.qty),
                "market_value": float(p.market_value),
                "unrealized_pl": float(p.unrealized_pl),
                "avg_entry_price": float(p.avg_entry_price),
                "current_price": float(p.current_price),
            }
            for p in positions
        ]

    def get_bars(self, ticker: str, timeframe: str = "1Day", limit: int = 30) -> list[dict]:
        """Return recent OHLCV bars from Alpaca for a single ticker.

        Args:
            ticker: Stock symbol.
            timeframe: Bar size string. "1Day" maps to TimeFrame.Day; anything
                else maps to TimeFrame.Hour.
            limit: Maximum number of bars to return.

        Returns:
            List of bar dicts with keys: timestamp, open, high, low, close, volume.
        """
        tf = TimeFrame.Day if "day" in timeframe.lower() else TimeFrame.Hour
        req = StockBarsRequest(symbol_or_symbols=ticker, timeframe=tf, limit=limit)
        bars = self._data.get_stock_bars(req)
        result = []
        for bar in bars.data.get(ticker, []):
            result.append({
                "timestamp": str(bar.timestamp),
                "open": float(bar.open),
                "high": float(bar.high),
                "low": float(bar.low),
                "close": float(bar.close),
                "volume": float(bar.volume),
            })
        return result

    def cancel_order(self, broker_order_id: str) -> bool:
        """Cancel an open Alpaca order by its broker ID.

        Args:
            broker_order_id: The UUID string assigned by Alpaca.

        Returns:
            True if the cancellation succeeded, False on error.
        """
        try:
            self._trading.cancel_order_by_id(broker_order_id)
            return True
        except Exception as e:
            logger.error(f"Failed to cancel order {broker_order_id}: {e}")
            return False

    def get_account_equity(self) -> float:
        """Return the total account equity from the Alpaca account.

        Returns:
            Account equity in USD as a float.
        """
        account = self._trading.get_account()
        return float(account.equity)

    def get_latest_price(self, ticker: str) -> Optional[float]:
        """Return the most recent close price for a ticker via Alpaca bars.

        Args:
            ticker: Stock symbol.

        Returns:
            Latest close price in USD, or None if the fetch fails.
        """
        try:
            bars = self.get_bars(ticker, timeframe="1Day", limit=1)
            return bars[-1]["close"] if bars else None
        except Exception as e:
            logger.warning(f"Could not fetch price for {ticker}: {e}")
            return None

    def get_options_chain(
        self, ticker: str, option_type: str, expiry_min_days: int = 25, expiry_max_days: int = 40
    ) -> list[dict]:
        """Fetch the options chain for a ticker filtered by expiry window and type.

        Targets monthly expirations 25 to 40 days out to avoid weekly theta crush
        while staying close enough for the signal to be actionable.

        Args:
            ticker: Underlying stock symbol.
            option_type: "call" or "put".
            expiry_min_days: Minimum days to expiration (inclusive).
            expiry_max_days: Maximum days to expiration (inclusive).

        Returns:
            List of contract dicts with keys: symbol, strike, expiry, bid, ask.
            Returns an empty list if the fetch fails.
        """
        now = datetime.now(timezone.utc)
        exp_after = (now + timedelta(days=expiry_min_days)).date()
        exp_before = (now + timedelta(days=expiry_max_days)).date()

        try:
            req = OptionChainRequest(
                underlying_symbol=ticker,
                expiration_date_gte=str(exp_after),
                expiration_date_lte=str(exp_before),
                type=option_type.lower(),
            )
            # API returns plain dict: {occ_symbol: OptionsSnapshot, ...}
            raw: dict = self._options_data.get_option_chain(req)
            contracts = []
            for occ_symbol, snap in raw.items():
                try:
                    # OCC format last 15 chars: YYMMDD + C/P + 8-digit strike*1000
                    # e.g. "AAPL260612C00185000" → expiry=2026-06-12, strike=185.0
                    suffix = occ_symbol[-15:]
                    expiry_date = datetime.strptime(suffix[:6], "%y%m%d").strftime("%Y-%m-%d")
                    strike = int(suffix[7:]) / 1000
                except Exception:
                    continue

                quote  = snap.latest_quote   # Quote object or None
                greeks = snap.greeks          # OptionsGreeks object or None

                contracts.append({
                    "symbol":             occ_symbol,
                    "strike":             strike,
                    "expiry":             expiry_date,
                    "bid":                float(getattr(quote,  "bid_price", 0) or 0),
                    "ask":                float(getattr(quote,  "ask_price",  0) or 0),
                    "open_interest":      int(getattr(snap,    "open_interest", 0) or 0),
                    "implied_volatility": float(snap.implied_volatility or 0),
                    "delta":              float(getattr(greeks, "delta", 0) or 0) if greeks else 0.0,
                    "gamma":              float(getattr(greeks, "gamma", 0) or 0) if greeks else 0.0,
                    "theta":              float(getattr(greeks, "theta", 0) or 0) if greeks else 0.0,
                })
            return contracts
        except Exception as e:
            logger.warning(f"Options chain fetch failed for {ticker}: {e}")
            return []

    def find_atm_contract(self, ticker: str, option_type: str) -> Optional[str]:
        """Find the nearest at-the-money options contract symbol for execution.

        Fetches the options chain for the configured expiry window and selects
        the contract whose strike price is closest to the current market price.

        Args:
            ticker: Underlying stock symbol.
            option_type: "call" or "put".

        Returns:
            OCC contract symbol string (e.g., "AAPL240119C00185000"), or None
            if the current price or options chain cannot be fetched.
        """
        current_price = self.get_latest_price(ticker)
        if not current_price:
            return None

        contracts = self.get_options_chain(ticker, option_type)
        if not contracts:
            return None

        atm = min(contracts, key=lambda c: abs(c["strike"] - current_price))
        return atm["symbol"]

    def find_atm_contract_full(self, ticker: str, option_type: str) -> Optional[dict]:
        """Return the full ATM contract dict enriched with pricing analytics.

        Fetches the options chain, picks the strike closest to market price, and
        computes intrinsic value, time value, moneyness, and days to expiry.

        Args:
            ticker: Underlying stock symbol.
            option_type: "call" or "put".

        Returns:
            Dict with all chain fields plus: current_price, mid, contract_value,
            intrinsic_value, time_value, itm (bool), days_to_expiry, multiplier.
            Returns None if price or chain cannot be fetched.
        """
        current_price = self.get_latest_price(ticker)
        if not current_price:
            return None

        contracts = self.get_options_chain(ticker, option_type)
        if not contracts:
            return None

        atm = min(contracts, key=lambda c: abs(c["strike"] - current_price))
        strike = atm["strike"]
        mid = (atm["bid"] + atm["ask"]) / 2
        is_call = option_type.lower() == "call"
        intrinsic = max(0.0, current_price - strike) if is_call else max(0.0, strike - current_price)
        itm = (current_price > strike) if is_call else (current_price < strike)

        days_to_expiry = None
        try:
            expiry_date = datetime.strptime(atm["expiry"], "%Y-%m-%d").date()
            days_to_expiry = (expiry_date - date_type.today()).days
        except Exception:
            pass

        return {
            **atm,
            "current_price": round(current_price, 2),
            "mid":            round(mid, 2),
            "contract_value": round(mid * 100, 2),
            "intrinsic_value": round(intrinsic, 2),
            "time_value":      round(max(0.0, mid - intrinsic), 2),
            "itm":             itm,
            "days_to_expiry":  days_to_expiry,
            "multiplier":      100,
        }

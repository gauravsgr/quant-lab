"""Order submission chokepoint for the trading agent.

All orders must go through submit_signal_order(). No other module is
permitted to call broker.submit_order() directly. This centralizes risk
checks, DB writes, and Slack notifications in one place.

Flow:
    1. Pre-trade validation (validator.py)
    2. Price fetch and position sizing (risk.py)
    3. Options contract lookup; falls back to equity if chain is unavailable
    4. Broker order submission
    5. DB write (signal + order)
    6. Slack notification
"""
from typing import Optional

from loguru import logger
from sqlalchemy.engine import Connection

from brokers.base import Broker
from brokers.alpaca import AlpacaBroker
from execution import risk, validator
from strategies.base import Signal


def submit_signal_order(
    signal: Signal,
    broker: Broker,
    db_conn: Connection,
    account_equity: float,
    min_confidence: float = 0.65,
    require_approval: bool = False,
    already_traded_tickers: Optional[set] = None,
    notifier=None,
) -> Optional[int]:
    """Validate a signal and submit an order to the broker.

    Returns the DB order.id on success, or None if the order was skipped,
    failed validation, or is in approval-pending mode. All side effects
    (DB write, Slack notification) happen here and nowhere else.

    Args:
        signal: The Signal instance to execute.
        broker: A Broker implementation (Alpaca paper/live or BacktestBroker).
        db_conn: Active SQLAlchemy connection for DB writes.
        account_equity: Current total account equity in USD.
        min_confidence: Minimum confidence threshold (default 0.65).
        require_approval: When True, notifies via Slack but does not submit the order.
        already_traded_tickers: Set of tickers traded today; used to prevent duplicates.
        notifier: Optional SlackNotifier instance for trade alerts.

    Returns:
        Integer DB order ID on success, None if skipped or failed.
    """
    import db.repository as repo

    try:
        validator.pre_trade_check(
            signal,
            min_confidence=min_confidence,
            require_approval=require_approval,
            already_traded_tickers=already_traded_tickers,
        )
    except validator.ValidationError as e:
        if require_approval and signal.signal_type != "NEUTRAL":
            # Approval mode: notify Slack without submitting to the broker.
            logger.info(f"Approval required for {signal.ticker}, notifying only")
            if notifier:
                notifier.send_signal_alert(signal, order=None, approval_pending=True)
        else:
            logger.info(str(e))
        return None

    current_price = broker.get_latest_price(signal.ticker)
    if not current_price:
        logger.warning(f"Cannot fetch price for {signal.ticker}, skipping order")
        return None

    notional = risk.max_order_value(account_equity)
    if not risk.check_position_size(account_equity, notional):
        logger.warning(f"{signal.ticker}: position size check failed")
        return None

    is_call = signal.signal_type == "STRONG_BUY"
    order_type_label = signal.order_type or ("call_option" if is_call else "put_option")

    try:
        if isinstance(broker, AlpacaBroker):
            option_type = "call" if is_call else "put"
            contract_symbol = broker.find_atm_contract(signal.ticker, option_type)

            if contract_symbol:
                # Options order: size by notional / (price * 100 per contract).
                contract_price = current_price
                contracts_qty = max(1, int(notional / (contract_price * 100)))
                order_result = broker.submit_options_order(
                    symbol=contract_symbol,
                    qty=contracts_qty,
                    side="buy",
                )
                entry_price = order_result.filled_price or current_price
                qty = float(contracts_qty)
                ticker_for_db = signal.ticker
            else:
                # Options chain unavailable; fall back to equity order.
                logger.warning(f"No options contract found for {signal.ticker}, using equity order")
                qty = risk.compute_qty_from_notional(notional, current_price)
                order_result = broker.submit_order(signal.ticker, qty, side="buy")
                entry_price = order_result.filled_price or current_price
                contract_symbol = None
                order_type_label = "equity_long" if is_call else "equity_short"
                ticker_for_db = signal.ticker
        else:
            qty = risk.compute_qty_from_notional(notional, current_price)
            order_result = broker.submit_order(
                signal.ticker, qty, side="buy" if is_call else "sell"
            )
            entry_price = order_result.filled_price or current_price
            ticker_for_db = signal.ticker

    except Exception as e:
        logger.error(f"Order submission failed for {signal.ticker}: {e}")
        return None

    stop_price = risk.compute_initial_stop(entry_price)

    signal_id = repo.insert_signal(
        db_conn,
        ticker=signal.ticker,
        signal_type=signal.signal_type,
        sentiment_score=signal.sentiment_score,
        politician_action=signal.politician_action,
        politician_name=signal.politician_name,
        politician_party=signal.politician_party,
        politician_chamber=signal.politician_chamber,
        politician_amount=signal.politician_amount,
        analyst_rating=signal.analyst_rating,
        analyst_buy_count=signal.analyst_buy_count,
        analyst_hold_count=signal.analyst_hold_count,
        analyst_sell_count=signal.analyst_sell_count,
        analyst_price_target=signal.analyst_price_target,
        news_headline=signal.news_headline,
        confidence=signal.confidence,
    )

    order_id = repo.insert_order(
        db_conn,
        signal_id=signal_id,
        ticker=ticker_for_db,
        order_type=order_type_label,
        broker_order_id=order_result.broker_order_id,
        qty=qty,
        entry_price=entry_price,
        stop_price=stop_price,
        trailing_stop_high=entry_price,
        status="open",
    )

    logger.info(
        f"Order recorded: {signal.signal_type} {signal.ticker} | "
        f"confidence={signal.confidence:.2f} | entry={entry_price} | stop={stop_price} | db_id={order_id}"
    )

    if notifier:
        notifier.send_signal_alert(
            signal,
            order={
                "broker_order_id": order_result.broker_order_id,
                "entry_price": entry_price,
                "stop_price": stop_price,
                "qty": qty,
                "notional": notional,
            },
        )

    return order_id

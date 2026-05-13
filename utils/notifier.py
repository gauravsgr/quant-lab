"""Slack notifications for trade signals and audit reports.

Sends rich Block Kit messages via the Slack Web API (chat.postMessage). When
REQUIRE_APPROVAL=true, signal alerts include interactive Approve/Reject buttons
that trigger order execution via Socket Mode callbacks in utils/slack_actions.py.

Typical usage:
    notifier = SlackNotifier(bot_token, channel_id)
    notifier.send_signal_alert(signal, order=order_dict)
"""
import urllib.parse
from datetime import datetime, timezone
from typing import Optional

from slack_sdk import WebClient
from slack_sdk.errors import SlackApiError
from loguru import logger

from strategies.base import Signal


def _bar(score: float, width: int = 10) -> str:
    """Render an ASCII progress bar representing a score in [0, 1].

    Args:
        score: A value between 0.0 and 1.0.
        width: Total number of characters in the bar.

    Returns:
        A string like '████████░░' where filled blocks represent the score.
    """
    filled = round(score * width)
    return "█" * filled + "░" * (width - filled)


def _reddit_url(ticker: str) -> str:
    """Build a Reddit recent-search URL for a ticker symbol."""
    q = urllib.parse.quote(f"${ticker}")
    return f"https://www.reddit.com/search/?q={q}&sort=new&t=week"


def _google_news_url(ticker: str) -> str:
    """Build a Google News search URL for a ticker symbol."""
    q = urllib.parse.quote(f"{ticker} stock")
    return f"https://news.google.com/search?q={q}"


def _capitol_trades_url(ticker: str, politician: Optional[str] = None) -> str:
    """Build a Capitol Trades disclosure search URL for a ticker symbol."""
    q = urllib.parse.quote(ticker)
    return f"https://capitoltrades.com/trades?ticker={q}"


class SlackNotifier:
    """Sends trade alerts and audit reports to a Slack channel via the Web API.

    Attributes:
        _client: slack_sdk WebClient authenticated with the bot token.
        _channel_id: Slack channel ID where all messages are posted.
    """

    def __init__(self, bot_token: str, channel_id: str):
        """Initialize the notifier with a Slack bot token and channel.

        Args:
            bot_token: Slack bot token (xoxb-...) from the Slack App configuration.
            channel_id: Slack channel ID (e.g. C0123456789) for trade alerts.
        """
        self._client = WebClient(token=bot_token)
        self._channel_id = channel_id

    def send_signal_alert(
        self,
        signal: Signal,
        order: Optional[dict] = None,
        approval_pending: bool = False,
        signal_id: Optional[int] = None,
    ) -> Optional[dict]:
        """Send a rich Block Kit signal alert to Slack.

        When approval_pending=True and signal_id is provided, two interactive
        buttons (Approve / Reject) are appended to the message. Clicking a button
        triggers the Socket Mode handler in utils/slack_actions.py.

        Args:
            signal: The Signal instance that triggered the alert.
            order: Optional dict with order details (qty, entry_price, stop_price,
                notional, broker_order_id). If None, shows a notification-only message.
            approval_pending: When True, adds an awaiting-approval label and buttons.
            signal_id: DB primary key of the signal; embedded in button action values
                so the callback can look up the signal and execute it on approval.

        Returns:
            A notification_metadata dict on success (platform-agnostic; stores what
            is needed to update the message later), or None on failure.
        """
        blocks = _build_signal_blocks(signal, order, approval_pending)
        if approval_pending and signal_id is not None:
            blocks += _build_approval_buttons(signal_id)
        try:
            resp = self._client.chat_postMessage(
                channel=self._channel_id,
                blocks=blocks,
                text=_signal_fallback_text(signal),
            )
            return {"platform": "slack", "ts": resp["ts"], "channel": resp["channel"]}
        except SlackApiError as e:
            logger.error(f"Slack signal alert failed: {e}")
            return None

    def update_approval_message(
        self,
        notification_metadata: dict,
        status: str,
        order_id: Optional[int] = None,
    ) -> None:
        """Replace an approval-pending message with a resolved status message.

        Removes the Approve/Reject buttons and shows the outcome. Called by the
        Socket Mode callback in utils/slack_actions.py after the user clicks a button.

        Args:
            notification_metadata: Dict returned by send_signal_alert (contains
                platform, ts, and channel for Slack).
            status: "approved", "rejected", or "failed".
            order_id: DB order ID to show in the approved message, if available.
        """
        ts = notification_metadata.get("ts")
        channel = notification_metadata.get("channel", self._channel_id)
        if not ts:
            logger.warning("update_approval_message: no ts in notification_metadata")
            return

        if status == "approved":
            text = f"APPROVED - Order #{order_id} submitted to Alpaca." if order_id else "APPROVED - Order submitted."
        elif status == "rejected":
            text = "REJECTED by user."
        else:
            text = "APPROVAL FAILED - see logs for details."

        blocks = [
            {"type": "divider"},
            {"type": "section", "text": {"type": "mrkdwn", "text": f"*{text}*"}},
            {"type": "divider"},
        ]
        try:
            self._client.chat_update(channel=channel, ts=ts, blocks=blocks, text=text)
        except SlackApiError as e:
            logger.error(f"Slack message update failed: {e}")

    def send_morning_summary_multi_strategy(
        self,
        signals_by_strategy: dict,
        aggregated: list,
        traded_count: int,
        tickers_scanned: int,
        pol_count: int,
        catalyst_count: int = 0,
        signal_id_map: Optional[dict] = None,
        options_ctx: Optional[dict] = None,
        require_approval: bool = False,
    ) -> None:
        """Send a single Slack message with all strategies' signals, options params, and action buttons.

        One chat_postMessage with up to 50 blocks. Structure:
            [1] Header with scan stats
            Per strategy (max 6, only those with signals):
                [1] Divider
                [1] Strategy title header
                [1] Compact table of all signals (mrkdwn rows)
                Per signal (up to 2):
                    [1] Signal detail card (company, rationale, options params, counter)
                    [1] Actions block (Approve/Reject buttons) or context link row

        Args:
            signals_by_strategy: Dict mapping strategy_name → list[Signal].
            aggregated: List of AggregatedSignal from SignalAggregator.
            traded_count: Orders actually submitted this cycle.
            tickers_scanned: Total watchlist tickers analyzed.
            pol_count: Number of Capitol Trades disclosures found.
            catalyst_count: Number of catalyst hits found.
            signal_id_map: ticker → {strategy_name → signal DB id} for button values.
            options_ctx: (ticker, "call"/"put") → full contract dict from find_atm_contract_full.
            require_approval: When True, show Approve/Reject buttons instead of executed status.
        """
        signal_id_map = signal_id_map or {}
        options_ctx = options_ctx or {}

        blocks: list = []

        # ── Header ────────────────────────────────────────────────────────────
        blocks += _build_single_message_header(
            signals_by_strategy, aggregated, traded_count,
            tickers_scanned, pol_count, catalyst_count,
        )

        STRATEGY_META = [
            ("technical",              "📈 TECHNICAL",          "RSI · MACD · Moving Averages"),
            ("sentiment",              "😄 SENTIMENT",           "Reddit · StockTwits"),
            ("political_news",         "🏛️ POLITICAL TRADES",   "Capitol Trades + News"),
            ("catalyst",               "🔍 CATALYST",            "Business Events · Gov Programs"),
            ("technical_buzz_ensemble","🔁 TECH+BUZZ ENSEMBLE",  "Technical × Sentiment"),
            ("full_confluence",        "🎯 FULL ENSEMBLE",       "All Sources Combined"),
        ]

        for strategy_name, title, subtitle in STRATEGY_META:
            if len(blocks) >= 46:
                break
            sigs = signals_by_strategy.get(strategy_name, [])
            actionable = [s for s in sigs if s.signal_type != "NEUTRAL"]
            if not actionable:
                continue

            blocks.append({"type": "divider"})
            blocks.append({
                "type": "header",
                "text": {"type": "plain_text", "text": f"{title}  ({subtitle})", "emoji": True},
            })

            # ASCII code-block grid table (all signals + options columns)
            blocks.append(_build_strategy_table_block(actionable, strategy_name, options_ctx))

            # Per-signal detail cards (max 2 per strategy to stay in block budget)
            detail_count = 0
            for sig in actionable:
                if detail_count >= 2 or len(blocks) >= 46:
                    break
                opt_type = "call" if sig.signal_type == "STRONG_BUY" else "put"
                opt = options_ctx.get((sig.ticker, opt_type))
                sid = signal_id_map.get(sig.ticker, {}).get(strategy_name)

                card_text = _build_signal_detail_card_v2(sig, strategy_name, aggregated, opt)
                blocks.append({
                    "type": "section",
                    "text": {"type": "mrkdwn", "text": card_text[:2900]},
                })

                if require_approval and sid is not None:
                    blocks += _build_approval_buttons(sid)
                else:
                    blocks.append({
                        "type": "context",
                        "elements": [{"type": "mrkdwn", "text": _build_signal_links(sig)}],
                    })

                detail_count += 1

        try:
            self._client.chat_postMessage(
                channel=self._channel_id,
                blocks=blocks[:50],
                text="Morning Scan Complete",
            )
        except SlackApiError as e:
            logger.error(f"Slack morning summary error: {e}")

    def send_morning_summary(
        self,
        all_signals: list,
        traded_count: int,
        tickers_scanned: int,
        pol_count: int,
    ) -> None:
        """Send a morning cycle summary to Slack, even when no trades are made.

        Args:
            all_signals: All Signal objects generated this morning.
            traded_count: Number of orders actually submitted.
            tickers_scanned: Total tickers fetched from Adanos.
            pol_count: Number of congressional disclosures found today.
        """
        blocks = _build_morning_summary_blocks(all_signals, traded_count, tickers_scanned, pol_count)
        try:
            self._client.chat_postMessage(
                channel=self._channel_id,
                blocks=blocks,
                text="Morning Scan Complete",
            )
        except SlackApiError as e:
            logger.error(f"Slack morning summary error: {e}")

    def send_daily_summary(self, closed_orders: list[dict], ghost_trades: list[dict]) -> None:
        """Send the end-of-day summary with closed positions and ghost trades.

        Args:
            closed_orders: List of order dicts for positions closed today by trailing stop.
            ghost_trades: List of performance dicts for signals that were not executed.
        """
        blocks = _build_daily_summary_blocks(closed_orders, ghost_trades)
        try:
            self._client.chat_postMessage(
                channel=self._channel_id,
                blocks=blocks,
                text="Daily Trading Summary",
            )
        except SlackApiError as e:
            logger.error(f"Slack daily summary error: {e}")

    def send_weekly_report(
        self,
        precision_data: dict,
        adanos_usage: dict,
        ghost_regrets: list[dict],
        best_performer: Optional[dict] = None,
    ) -> None:
        """Send the Friday weekly precision audit report to Slack.

        Args:
            precision_data: Dict from get_weekly_signal_precision: {order_type: stats}.
            adanos_usage: Dict with call_count for the current month.
            ghost_regrets: List of ghost trade performance rows for the week.
            best_performer: Optional order dict for the best open position by unrealized P&L.
        """
        blocks = _build_weekly_report_blocks(precision_data, adanos_usage, ghost_regrets, best_performer)
        try:
            self._client.chat_postMessage(
                channel=self._channel_id,
                blocks=blocks,
                text="Weekly Signal Audit Report",
            )
        except SlackApiError as e:
            logger.error(f"Slack weekly report error: {e}")


# ---------------------------------------------------------------------------
# Block Kit builders
# ---------------------------------------------------------------------------

def _signal_fallback_text(signal: Signal) -> str:
    """Build the plain-text fallback for clients that cannot render Block Kit.

    Args:
        signal: The Signal instance to describe.

    Returns:
        A compact single-line summary string.
    """
    emoji = "🟢" if signal.signal_type == "STRONG_BUY" else "🔴"
    ot = signal.order_type.replace("_", " ").upper() if signal.order_type else signal.signal_type
    return f"{emoji} ${signal.ticker} | {ot} | Confidence {signal.confidence:.0%}"


def _build_signal_blocks(signal: Signal, order: Optional[dict], approval_pending: bool) -> list:
    """Build the full five-section Block Kit block list for a signal alert.

    Sections: header, executive summary, signal breakdown, contextual news,
    counter-considerations reviewed, and research links.

    Args:
        signal: The Signal instance to describe.
        order: Optional order execution details dict.
        approval_pending: When True, the header shows an awaiting-approval label.

    Returns:
        List of Slack Block Kit block dicts ready for chat_postMessage.
    """
    now = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M EST")
    is_buy = signal.signal_type == "STRONG_BUY"
    emoji = "🟢" if is_buy else "🔴"
    direction = "STRONG BUY" if is_buy else "STRONG PUT"
    order_label = (signal.order_type or "").replace("_", " ").upper()

    header_text = f"{emoji}  ${signal.ticker}  |  {direction}  |  {order_label}"
    if approval_pending:
        header_text += "  - AWAITING APPROVAL"

    if order:
        exec_detail = (
            f"*Action:* Buy {order.get('qty', '?')} contract(s)  ·  "
            f"*Entry:* ${order.get('entry_price', '?'):.2f}  ·  "
            f"*Stop:* ${order.get('stop_price', '?'):.2f}  ·  "
            f"*Notional:* ${order.get('notional', 0):,.0f}"
        )
    else:
        exec_detail = "_(Notified only, not executed)_" if approval_pending else "_(No order details)_"

    summary = _build_executive_summary(signal, is_buy, order)
    breakdown = _build_signal_breakdown(signal)
    counters = _build_counter_considerations(signal, is_buy)

    links = (
        f"<{_reddit_url(signal.ticker)}|Reddit Search>  ·  "
        f"<{_google_news_url(signal.ticker)}|Google News>"
    )
    if signal.disclosure_url:
        links += f"  ·  <{signal.disclosure_url}|Capitol Trades Disclosure>"

    blocks = [
        {"type": "divider"},
        {
            "type": "header",
            "text": {"type": "plain_text", "text": header_text, "emoji": True},
        },
        {
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*Confidence Score:* {signal.confidence:.2f} / 1.00   ·   {now}",
            },
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*📋 EXECUTIVE SUMMARY*\n{summary}"},
        },
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": exec_detail},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*📊 SIGNAL BREAKDOWN*\n\n{breakdown}"},
        },
    ]

    if signal.news_headline:
        blocks.append({
            "type": "section",
            "text": {
                "type": "mrkdwn",
                "text": f"*📰 CONTEXTUAL NEWS*\n_{signal.news_headline}_",
            },
        })

    blocks += [
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*⚖️ COUNTER-CONSIDERATIONS REVIEWED*\n{counters}"},
        },
        {"type": "divider"},
        {
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*🔗 RESEARCH LINKS*\n{links}"},
        },
        {"type": "divider"},
    ]

    return blocks


def _build_approval_buttons(signal_id: int) -> list:
    """Build an actions block with Approve and Reject buttons for a pending signal.

    The signal_id is embedded in each button's value so the Socket Mode callback
    in utils/slack_actions.py can look up and execute the correct signal.

    Args:
        signal_id: DB primary key of the signal awaiting approval.

    Returns:
        A list containing one Slack actions block.
    """
    return [
        {
            "type": "actions",
            "elements": [
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Approve", "emoji": False},
                    "style": "primary",
                    "action_id": "approve_signal",
                    "value": str(signal_id),
                },
                {
                    "type": "button",
                    "text": {"type": "plain_text", "text": "Reject", "emoji": False},
                    "style": "danger",
                    "action_id": "reject_signal",
                    "value": str(signal_id),
                },
            ],
        }
    ]


def _build_executive_summary(signal: Signal, is_buy: bool, order: Optional[dict]) -> str:
    """Build a natural-language executive summary paragraph for a signal.

    Args:
        signal: The Signal instance to summarize.
        is_buy: True for STRONG_BUY, False for STRONG_PUT.
        order: Optional order dict with notional amount.

    Returns:
        A multi-sentence summary string in Slack mrkdwn format.
    """
    direction = "long" if is_buy else "short/put"
    signals_present = []
    if signal.sentiment_score is not None:
        sentiment_word = "positive" if signal.sentiment_score > 0 else "negative"
        signals_present.append(f"extreme {sentiment_word} social sentiment ({signal.sentiment_score:.2f})")
    if signal.politician_name:
        signals_present.append(f"congressional {signal.politician_action or 'activity'} by {signal.politician_name}")
    if signal.analyst_rating in ("Strong Buy", "Buy", "Sell", "Strong Sell"):
        signals_present.append(f"analyst consensus: {signal.analyst_rating}")

    confluence_desc = " + ".join(signals_present) if signals_present else "alternative data confluence"

    summary = (
        f"*${signal.ticker}* has triggered a high-conviction {direction} signal driven by "
        f"{confluence_desc}. "
    )
    if len(signals_present) >= 2:
        summary += (
            "All independent signals point in the same direction simultaneously, "
            "a rare setup that warrants action. "
        )
    if order:
        notional = order.get("notional", 0)
        summary += (
            f"Position size: ${notional:,.0f} (5% of account). "
            f"Trailing stop: 10% from highest close."
        )
    return summary


def _build_signal_breakdown(signal: Signal) -> str:
    """Build the per-source signal breakdown section with ASCII bars.

    Args:
        signal: The Signal instance with sentiment, politician, and analyst data.

    Returns:
        A mrkdwn string with one sub-section per data source that contributed.
    """
    parts = []

    tech_score = getattr(signal, "technical_score", None)
    tech_dir = getattr(signal, "technical_direction", None)
    tech_rsi = getattr(signal, "technical_rsi", None)
    tech_macd = signal.components.get("macd") if signal.components else None
    tech_ma = signal.components.get("ma") if signal.components else None
    if tech_score is not None and tech_dir:
        tech_label = "BULLISH" if tech_dir == "bullish" else ("BEARISH" if tech_dir == "bearish" else "NEUTRAL")
        bar = _bar(max(0.0, tech_score) if tech_score >= 0 else max(0.0, -tech_score))
        details = [
            f"Composite score: {tech_score:+.2f} -> *{tech_label}*",
            f"`{bar}`",
        ]
        if tech_rsi is not None:
            rsi_note = "oversold" if tech_rsi < 35 else ("overbought" if tech_rsi > 65 else "neutral zone")
            details.append(f"RSI(14): {tech_rsi:.1f} ({rsi_note})")
        if tech_macd:
            details.append(f"MACD histogram: {tech_macd}")
        if tech_ma:
            details.append(f"Price vs 20-day MA: {tech_ma}")
        parts.append(f"*Technical Analysis (weight 50%)*\n" + "\n".join(details))

    if signal.sentiment_score is not None:
        s = signal.sentiment_score
        direction = "BULLISH" if s > 0 else "BEARISH"
        bar = _bar(max(0.0, s) if s > 0 else max(0.0, -s))
        buzz_line = f"Adanos sentiment score: {s:+.2f} -> *{direction}*\n`{bar}`"
        parts.append(f"*Social Sentiment (weight 25%)*\n{buzz_line}")

    if signal.politician_name:
        pa = (signal.politician_action or "").upper()
        pol_dir = "BULLISH" if pa in ("BUY", "PURCHASE") else "BEARISH"
        bar = _bar(1.0 if pol_dir == "BULLISH" else 0.0)
        details = [f"{pa} -> *{pol_dir}*", f"`{bar}`"]
        if signal.politician_name:
            details.append(f"*{signal.politician_name}*")
        if signal.politician_party or signal.politician_chamber:
            meta = " · ".join(filter(None, [signal.politician_party, signal.politician_chamber]))
            details.append(meta)
        if signal.politician_amount:
            details.append(f"Amount: {signal.politician_amount}")
        parts.append(f"*Political Activity (bonus)*\n" + "\n".join(details))

    if signal.analyst_rating:
        total = signal.analyst_buy_count + signal.analyst_hold_count + signal.analyst_sell_count
        buy_pct = signal.analyst_buy_count / total if total else 0
        bar = _bar(buy_pct)
        analyst_dir = "BULLISH" if signal.analyst_rating in ("Strong Buy", "Buy") else (
            "BEARISH" if signal.analyst_rating in ("Sell", "Strong Sell") else "NEUTRAL"
        )
        details = [
            f"{signal.analyst_rating} -> *{analyst_dir}*",
            f"`{bar}`",
            f"Last 30 days: {signal.analyst_buy_count} Buy · {signal.analyst_hold_count} Hold · {signal.analyst_sell_count} Sell",
        ]
        if signal.analyst_price_target:
            details.append(f"Mean price target: ${signal.analyst_price_target:.2f}")
        parts.append(f"*Analyst Consensus (weight 25%)*\n" + "\n".join(details))  # noqa: E501

    return "\n\n".join(parts) if parts else "_No signal breakdown available_"


def _build_counter_considerations(signal: Signal, is_buy: bool) -> str:
    """Build the counter-considerations section showing risks that were reviewed.

    Args:
        signal: The Signal instance (used to check sentiment level for crowding risk).
        is_buy: True for bullish signals, False for bearish.

    Returns:
        A mrkdwn string listing each risk considered and its offset.
    """
    counters = []

    if is_buy:
        counters.append(
            "X  *Disclosure lag risk:* Congressional trades are disclosed up to 45 days after execution. "
            "-> _Offset by: real-time sentiment spike and analyst consensus provide independent corroboration._"
        )
        if signal.sentiment_score and signal.sentiment_score > 0.85:
            counters.append(
                "X  *Crowded trade risk:* Very high social buzz may indicate a crowded long. "
                "-> _Mitigated by: trailing stop at 10% limits downside if sentiment reverses._"
            )
        counters.append(
            "X  *Options premium risk:* IV may be elevated if buzz correlates with upcoming events. "
            "-> _Mitigated by: 25-40 day expiry avoids weekly IV crush; theta decay manageable._"
        )
        counters.append(
            "*Net assessment:* Risks present but do not negate the three-way confluence signal. "
            "Position sized conservatively at 5% of account equity."
        )
    else:
        counters.append(
            "X  *Short squeeze risk:* Bearish political disclosures can be contrarian indicators. "
            "-> _Offset by: negative sentiment and analyst downgrade provide independent confirmation._"
        )
        counters.append(
            "X  *Limited downside capture:* Puts require sustained downward movement before expiry. "
            "-> _Mitigated by: 25-40 day expiry provides sufficient time window._"
        )
        counters.append(
            "*Net assessment:* Downside risks acknowledged; three-way bearish confluence justifies put position."
        )

    return "\n".join(counters)


def _fmt_expiry(expiry: str) -> str:
    """Format a YYYY-MM-DD expiry as 'Jun 20' for compact display."""
    try:
        dt = datetime.strptime(expiry, "%Y-%m-%d")
        return dt.strftime("%b %-d")
    except Exception:
        return expiry


def _build_signal_links(sig) -> str:
    """Build a compact link row for a signal (news article URL + Reddit + Capitol Trades)."""
    parts = []
    if getattr(sig, "news_url", None):
        headline_short = (sig.news_headline or "Article")[:50]
        parts.append(f"<{sig.news_url}|📰 {headline_short}>")
    else:
        parts.append(f"<{_google_news_url(sig.ticker)}|📰 Google News>")
    parts.append(f"<{_reddit_url(sig.ticker)}|💬 Reddit>")
    if getattr(sig, "disclosure_url", None):
        parts.append(f"<{sig.disclosure_url}|🏛️ Capitol Trades>")
    return "  ·  ".join(parts)


def _build_strategy_table_block(signals: list, strategy_name: str, options_ctx: dict) -> dict:
    """Build a Slack section block containing a monospace ASCII grid table.

    Every row has: Ticker | Company | Dir | Conf | [strategy cols] | Opt | Strike | Expiry | Prem | Mono | Delta | IV
    Options columns show 'N/A' when no contract data is available.
    Rendered as a triple-backtick code block for fixed-width alignment.
    """
    # ── Column definitions ────────────────────────────────────────────────────
    BASE_HDRS = ["Ticker", "Company             ", "Dir", "Conf"]
    BASE_W    = [6,         20,                    3,     4   ]

    if strategy_name in ("technical", "technical_buzz_ensemble", "full_confluence"):
        MID_HDRS = ["RSI ", "MACD", "MA  "]
        MID_W    = [4,       4,      4   ]
    elif strategy_name == "sentiment":
        MID_HDRS = ["Sent  ", "Buzz "]
        MID_W    = [6,         5    ]
    elif strategy_name == "political_news":
        MID_HDRS = ["Politician         ", "Chm"]
        MID_W    = [19,                     3  ]
    elif strategy_name == "catalyst":
        MID_HDRS = ["CatType    ", "Prog   "]
        MID_W    = [11,             7      ]
    else:
        MID_HDRS, MID_W = [], []

    OPT_HDRS = ["Opt ", "Strike", "Expiry", "Prem ", "Mono", "Delta ", "IV%  "]
    OPT_W    = [4,       6,        6,        5,       4,      6,        5     ]

    ALL_HDRS = BASE_HDRS + MID_HDRS + OPT_HDRS
    ALL_W    = BASE_W    + MID_W    + OPT_W

    def row(*cells):
        return "  ".join(str(c).ljust(w)[:w] for c, w in zip(cells, ALL_W))

    sep = "  ".join("-" * w for w in ALL_W)
    MAX_ROWS = 20  # keeps table well under Slack's 3000-char block limit
    lines = [row(*ALL_HDRS), sep]

    for sig in signals[:MAX_ROWS]:
        opt_type = "call" if sig.signal_type == "STRONG_BUY" else "put"
        opt = options_ctx.get((sig.ticker, opt_type)) or {}
        comps = sig.components or {}

        # Base columns
        ticker  = sig.ticker[:6]
        company = (sig.company_name or sig.ticker)[:20]
        direct  = "BUY" if sig.signal_type == "STRONG_BUY" else "PUT"
        conf    = f"{sig.confidence:.2f}"

        # Strategy-specific columns
        if strategy_name in ("technical", "technical_buzz_ensemble", "full_confluence"):
            rsi  = f"{sig.technical_rsi:.0f}" if sig.technical_rsi else "N/A"
            macd_raw = str(comps.get("macd", "")).lower()
            macd = "Bull" if "bull" in macd_raw else ("Bear" if "bear" in macd_raw else "Neut")
            ma_raw = str(comps.get("ma", "")).lower()
            ma   = "Abv" if "above" in ma_raw else ("Blw" if "below" in ma_raw else "N/A")
            mid_vals = [rsi, macd, ma]
        elif strategy_name == "sentiment":
            sent = f"{sig.sentiment_score:+.2f}" if sig.sentiment_score is not None else "N/A"
            buzz = str(comps.get("buzz_score", "N/A"))[:5]
            mid_vals = [sent, buzz]
        elif strategy_name == "political_news":
            pol = (sig.politician_name or "?")[:19]
            chm = (sig.politician_chamber or "?")[:3]
            mid_vals = [pol, chm]
        elif strategy_name == "catalyst":
            cat  = (sig.catalyst_type or "?")[:11]
            prog = (sig.program_match or "?")[:7]
            mid_vals = [cat, prog]
        else:
            mid_vals = []

        # Options columns — show N/A when data is unavailable
        if opt:
            o_type  = opt_type.upper()[:4]
            strike  = f"${opt.get('strike', 0):.0f}"
            expiry  = _fmt_expiry(opt.get("expiry", ""))[:6]
            prem    = f"${opt.get('mid', 0):.2f}"
            mono    = "ITM" if opt.get("itm") else "OTM"
            delta   = f"{opt.get('delta', 0):+.2f}"
            iv_raw  = opt.get("implied_volatility", 0)
            iv_str  = f"{iv_raw * 100:.0f}%" if iv_raw < 5 else f"{iv_raw:.0f}%"
        else:
            o_type = strike = expiry = prem = mono = delta = iv_str = "N/A"

        lines.append(row(ticker, company, direct, conf, *mid_vals, o_type, strike, expiry, prem, mono, delta, iv_str))

    if len(signals) > MAX_ROWS:
        lines.append(f"... {len(signals) - MAX_ROWS} more signals (showing top {MAX_ROWS})")

    table_text = "```\n" + "\n".join(lines) + "\n```"
    # Slack hard limit is 3000 chars; truncate body rows if still over (edge case)
    if len(table_text) > 2990:
        table_text = table_text[:2980] + "\n```"
    return {"type": "section", "text": {"type": "mrkdwn", "text": table_text}}


def _build_single_message_header(
    signals_by_strategy: dict,
    aggregated: list,
    traded_count: int,
    tickers_scanned: int,
    pol_count: int,
    catalyst_count: int,
) -> list:
    """Build the 3-block header section for the single-message morning summary."""
    date_str = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M ET")

    total_actionable = sum(
        len([s for s in sigs if s.signal_type != "NEUTRAL"])
        for sigs in signals_by_strategy.values()
        if isinstance(sigs, list)
    )
    high_conviction = sum(1 for ag in aggregated if ag.agreement_count >= 2 and not ag.conflict)
    conflict_count = sum(1 for ag in aggregated if ag.conflict)

    stats = (
        f"*{tickers_scanned}* tickers   *{pol_count}* disclosures   "
        f"*{catalyst_count}* catalysts   *{total_actionable}* signals   "
        f"*{traded_count}* trades"
    )

    conviction_parts = []
    for ag in aggregated:
        if ag.agreement_count >= 2 and not ag.conflict:
            icon = "🟢" if ag.final_signal_type == "STRONG_BUY" else "🔴"
            conviction_parts.append(f"{icon} *{ag.ticker}* ({ag.agreement_count}×, conf={ag.final_confidence:.2f})")
    conviction_str = "  ".join(conviction_parts[:8]) if conviction_parts else "_None_"

    conflict_parts = [f"⚠️ *{ag.ticker}*" for ag in aggregated if ag.conflict]
    conflict_str = "  ".join(conflict_parts[:5]) if conflict_parts else "_None_"

    detail = f"*High conviction:* {conviction_str}"
    if conflict_count:
        detail += f"\n*Conflicted (skipped):* {conflict_str}"

    return [
        {"type": "divider"},
        {
            "type": "header",
            "text": {"type": "plain_text", "text": f"📊 Morning Scan — {date_str}", "emoji": True},
        },
        {"type": "section", "text": {"type": "mrkdwn", "text": stats}},
        {"type": "section", "text": {"type": "mrkdwn", "text": detail}},
    ]


def _build_signal_detail_card_v2(sig, strategy_name: str, aggregated: list, opt: Optional[dict]) -> str:
    """Build the full mrkdwn text for a single signal detail card.

    Includes: company name/desc, signal direction, options contract parameters
    (strike, expiry, premium, ITM/OTM, intrinsic, time value, IV, delta, gamma,
    theta, multiplier, contract value), signal rationale, and counter-argument.
    """
    icon = "🟢" if sig.signal_type == "STRONG_BUY" else "🔴"
    direction = "BUY (Call)" if sig.signal_type == "STRONG_BUY" else "PUT (Put)"
    name = sig.company_name or sig.ticker
    desc = (sig.company_description or "").strip()

    # Ensemble agreement note
    ag = next((a for a in aggregated if a.ticker == sig.ticker), None)
    ensemble_note = ""
    if ag and ag.agreement_count >= 2:
        ensemble_note = f"  ✅ *{ag.agreement_count} strategies agree* (ensemble conf={ag.final_confidence:.2f})"
    elif ag and ag.conflict:
        ensemble_note = "  ⚠️ *Conflicted with another strategy*"

    lines = [
        f"{icon} *{sig.ticker} — {name}*{ensemble_note}",
        f"*Signal:* {direction}  |  *Confidence:* {sig.confidence:.2f}",
    ]
    if desc:
        lines.append(f"*Company:* {desc}")

    # Signal rationale
    reasons = []
    if sig.technical_score is not None:
        rsi = f"RSI={sig.technical_rsi:.0f}" if sig.technical_rsi else ""
        macd = sig.components.get("macd", "") if sig.components else ""
        ma = sig.components.get("ma", "") if sig.components else ""
        reasons.append(
            f"Technical: score={sig.technical_score:+.2f} ({sig.technical_direction})"
            + (f", {rsi}" if rsi else "")
            + (f", MACD={macd}" if macd else "")
            + (f", price {ma} 20MA" if ma else "")
        )
    if sig.sentiment_score is not None:
        reasons.append(f"Sentiment: {sig.sentiment_score:+.2f}")
    if sig.politician_name:
        reasons.append(
            f"Political: {sig.politician_name} ({sig.politician_party or '?'}, "
            f"{sig.politician_chamber or '?'}) "
            f"{(sig.politician_action or '').upper()} {sig.politician_amount or ''}"
        )
    if sig.catalyst_summary:
        reasons.append(f"Catalyst: {sig.catalyst_summary}")
    if sig.analyst_rating:
        reasons.append(
            f"Analysts: {sig.analyst_rating} "
            f"({sig.analyst_buy_count}B/{sig.analyst_hold_count}H/{sig.analyst_sell_count}S)"
        )
    if sig.news_headline:
        reasons.append(f"News: _{sig.news_headline}_")

    if reasons:
        lines.append("\n*Signal reason:*\n" + "\n".join(f"• {r}" for r in reasons))

    # Options contract block
    if not opt:
        lines.append("\n*📊 Options Contract:* _No active chain found for this ticker (market may be closed or contract unavailable)_")
    elif opt:
        opt_type_label = "CALL" if sig.signal_type == "STRONG_BUY" else "PUT"
        strike = opt.get("strike", 0)
        expiry = opt.get("expiry", "")
        mid = opt.get("mid", 0)
        cv = opt.get("contract_value", 0)
        intrinsic = opt.get("intrinsic_value", 0)
        tv = opt.get("time_value", 0)
        itm = opt.get("itm", False)
        dte = opt.get("days_to_expiry")
        iv = opt.get("implied_volatility", 0)
        delta = opt.get("delta", 0)
        gamma = opt.get("gamma", 0)
        theta = opt.get("theta", 0)
        mult = opt.get("multiplier", 100)
        price = opt.get("current_price", 0)
        moneyness = "ITM" if itm else "OTM"
        dte_str = f" ({dte}d)" if dte is not None else ""
        iv_pct = f"{iv * 100:.1f}%" if iv < 5 else f"{iv:.1f}%"
        lines.append(
            f"\n*📊 Options Contract (ATM {opt_type_label}):*\n"
            f"Symbol: `{opt.get('symbol', '?')}`  |  Strike: *${strike:.2f}*  |  "
            f"Expiry: {_fmt_expiry(expiry)}{dte_str}\n"
            f"Stock: ${price:.2f}  |  Premium (mid): *${mid:.2f}*  |  "
            f"Contract value: ${cv:.0f}  |  Multiplier: {mult}×\n"
            f"Moneyness: *{moneyness}*  |  Intrinsic: ${intrinsic:.2f}  |  "
            f"Time value: ${tv:.2f}\n"
            f"IV: {iv_pct}  |  Delta: {delta:+.3f}  |  "
            f"Gamma: {gamma:.4f}  |  Theta: {theta:+.3f}/day"
        )

    # Counter-argument
    counter = _build_counter_for_signal(sig)
    lines.append(
        f"\n*Counter:* {counter['argument']}\n"
        f"*Dismissed:* {counter['dismissed']}"
    )

    return "\n".join(lines)


def _build_counter_for_signal(sig) -> dict:
    """Generate a context-appropriate counter-argument and dismissal for a signal."""
    strategy = getattr(sig, "strategy_name", "")
    is_buy = sig.signal_type == "STRONG_BUY"

    if strategy == "technical":
        if is_buy:
            arg = "Technical momentum can reverse quickly if macro news breaks against the position."
            dismissed = "Signal is short-term (25-40 day options); 10% trailing stop caps downside. Mean-reversion setups don't require macro tailwind."
        else:
            arg = "Bearish technicals can produce false negatives if broader market rallies."
            dismissed = "MACD + RSI double-confirmation reduces false signals. Options expiry managed separately."
    elif strategy == "sentiment":
        if is_buy:
            arg = "Reddit buzz is noisy; retail-driven moves can reverse within hours."
            dismissed = "Used as a confirming signal only. Analyst consensus provides independent corroboration."
        else:
            arg = "Negative sentiment can be contrarian — bottoms sometimes form at peak pessimism."
            dismissed = "Analyst consensus aligns; this is not a single-source bearish call."
    elif strategy == "political_news":
        arg = "Congressional disclosures lag by up to 45 days; the move may already be priced in."
        dismissed = "News confirmation required — recent headline (< 7 days) shows thesis is still active."
    elif strategy == "catalyst":
        if sig.program_match:
            arg = f"Being a {sig.program_match} beneficiary doesn't guarantee revenue this quarter."
            dismissed = "Gov program timelines are multi-year; managed with short-dated options to limit time risk."
        else:
            arg = "Catalyst news may already be priced in by institutional investors."
            dismissed = "Keyword match in recent headline (< 5 days). RSI checked against technical layer for confirmation."
    elif strategy in ("technical_buzz_ensemble", "full_confluence"):
        n_agree = (sig.components or {}).get("agreement_count", 1)
        arg = f"Multi-strategy agreement ({n_agree} strategies) can create false confidence in correlated signals."
        dismissed = "Strategies use independent data sources (charts, Reddit, Capitol Trades, Alpaca News) — genuine confirmation."
    else:
        arg = "Signal may be based on incomplete or lagging data."
        dismissed = "Risk managed via conservative position sizing (5% of equity) and trailing stop."

    return {"argument": arg, "dismissed": dismissed}


def _build_morning_summary_blocks(
    all_signals: list,
    traded_count: int,
    tickers_scanned: int,
    pol_count: int,
) -> list:
    """Build Block Kit blocks for the morning cycle summary.

    Always sent regardless of whether trades were made. Shows scan stats,
    top signals by confidence, and a plain-language reason if nothing traded.
    """
    from strategies.base import Signal
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M UTC")

    actionable = [s for s in all_signals if s.signal_type != "NEUTRAL"]
    neutral = [s for s in all_signals if s.signal_type == "NEUTRAL"]
    top5 = sorted(all_signals, key=lambda s: s.confidence, reverse=True)[:5]

    if traded_count > 0:
        headline = f"Morning Scan — {traded_count} trade{'s' if traded_count != 1 else ''} executed"
    elif actionable:
        headline = "Morning Scan — signals found but not traded (approval pending or below threshold)"
    else:
        headline = "Morning Scan — no actionable signals today"

    stats = (
        f"*Tickers scanned:* {tickers_scanned}   "
        f"*Congressional disclosures:* {pol_count}   "
        f"*Actionable signals:* {len(actionable)}   "
        f"*Trades executed:* {traded_count}"
    )

    top_lines = []
    for s in top5:
        icon = "🟢" if s.signal_type == "STRONG_BUY" else ("🔴" if s.signal_type == "STRONG_PUT" else "⚪")
        sentiment_str = f"{s.sentiment_score:+.2f}" if s.sentiment_score is not None else "n/a"
        pol_str = s.politician_action or "none"
        analyst_str = s.analyst_rating or "none"
        top_lines.append(
            f"{icon} *{s.ticker}*  conf={s.confidence:.2f}  "
            f"sent={sentiment_str}  pol={pol_str}  analyst={analyst_str}"
        )

    if not actionable and not traded_count:
        if not pol_count:
            reason = (
                "No congressional disclosures in the last 3 days (politician weight = 0 for all tickers). "
                "Confidence scores stay low without that component. No tickers crossed the 0.65 threshold."
            )
        else:
            reason = "Sentiment scores were below the ±0.70 threshold required to trigger a STRONG_BUY or STRONG_PUT signal."
    else:
        reason = None

    blocks = [
        {"type": "divider"},
        {"type": "header", "text": {"type": "plain_text", "text": f"📊 {headline}", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": f"_{date}_\n\n{stats}"}},
    ]

    if top5:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": "*Top 5 signals by confidence:*\n" + "\n".join(top_lines)},
        })

    if reason:
        blocks.append({"type": "divider"})
        blocks.append({
            "type": "section",
            "text": {"type": "mrkdwn", "text": f"*Why no trades:* {reason}"},
        })

    blocks.append({"type": "divider"})
    return blocks


def _build_daily_summary_blocks(closed_orders: list[dict], ghost_trades: list[dict]) -> list:
    """Build Block Kit blocks for the end-of-day summary message.

    Args:
        closed_orders: List of order dicts for positions closed today by trailing stop.
        ghost_trades: List of performance dicts for signals not executed today.

    Returns:
        List of Slack Block Kit block dicts.
    """
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")
    lines = [f"*Daily Summary - {date}*\n"]

    if closed_orders:
        lines.append(f"*Closed positions today:* {len(closed_orders)}")
        for o in closed_orders:
            pnl = o.get("pnl", 0) or 0
            sign = "+" if pnl >= 0 else ""
            lines.append(f"  * {o['ticker']}: P&L {sign}${pnl:,.2f} (stop triggered)")
    else:
        lines.append("No positions closed today.")

    if ghost_trades:
        lines.append(f"\n*Ghost trades ({len(ghost_trades)} signals not executed):*")
        for g in ghost_trades[:5]:
            lines.append(f"  * {g['ticker']} at mark ${g['mark_price']:.2f}")

    return [
        {"type": "divider"},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(lines)}},
        {"type": "divider"},
    ]


def _build_weekly_report_blocks(
    precision_data: dict,
    adanos_usage: dict,
    ghost_regrets: list[dict],
    best_performer: Optional[dict],
) -> list:
    """Build Block Kit blocks for the Friday weekly precision audit report.

    Args:
        precision_data: Dict from get_weekly_signal_precision: {order_type: stats}.
        adanos_usage: Dict with call_count for the current month.
        ghost_regrets: List of ghost performance rows for the week.
        best_performer: Optional order dict for the best open position.

    Returns:
        List of Slack Block Kit block dicts.
    """
    date = datetime.now(timezone.utc).strftime("%Y-%m-%d")

    rows = ["```", f"{'Source':<20} {'Signals':>7} {'Won':>5} {'Lost':>5} {'Ghost':>6} {'Win Rate':>9}", "-" * 55]
    total_signals = total_won = 0
    for source, stats in precision_data.items():
        label = source.replace("_", " ").title()
        ghost = stats.get("ghost", 0)
        rows.append(
            f"{label:<20} {stats['signals']:>7} {stats['won']:>5} {stats['lost']:>5} "
            f"{ghost:>6} {stats['win_rate']:>8.1f}%"
        )
        total_signals += stats["signals"]
        total_won += stats["won"]

    rows.append("-" * 55)
    overall_rate = round(total_won / total_signals * 100, 1) if total_signals else 0.0
    rows.append(f"{'Overall (executed)':<20} {total_signals:>7} {total_won:>5} {total_signals - total_won:>5} {'':>6} {overall_rate:>8.1f}%")
    rows.append("```")

    summary_lines = [f"*Weekly Signal Audit - Week ending {date}*\n", "\n".join(rows)]

    if best_performer:
        pnl = best_performer.get("unrealized_pnl", 0)
        summary_lines.append(
            f"\n*Best performer:* ${best_performer['ticker']} "
            f"({'+' if pnl >= 0 else ''}{pnl:,.2f} unrealized)"
        )

    if ghost_regrets:
        top_regret = sorted(ghost_regrets, key=lambda g: abs(g.get("unrealized_pnl", 0)), reverse=True)[:1]
        if top_regret:
            g = top_regret[0]
            summary_lines.append(
                f"*Ghost trade note:* ${g['ticker']} would have returned "
                f"${g.get('unrealized_pnl', 0):+,.2f} (not traded)"
            )

    month = datetime.now(timezone.utc).strftime("%Y-%m")
    used = adanos_usage.get("call_count", 0)
    remaining = 250 - used
    summary_lines.append(
        f"\n*Adanos API budget:* {used} calls used this month, {remaining} remaining of 250 free"
    )

    return [
        {"type": "divider"},
        {"type": "header", "text": {"type": "plain_text", "text": "Weekly Signal Audit", "emoji": True}},
        {"type": "section", "text": {"type": "mrkdwn", "text": "\n".join(summary_lines)}},
        {"type": "divider"},
    ]

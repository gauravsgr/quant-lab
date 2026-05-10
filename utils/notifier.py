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
    """Build a Reddit hot-search URL for a ticker symbol."""
    q = urllib.parse.quote(f"${ticker}")
    return f"https://www.reddit.com/search/?q={q}&sort=hot"


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

    if signal.sentiment_score is not None:
        s = signal.sentiment_score
        direction = "BULLISH" if s > 0 else "BEARISH"
        bar = _bar(max(0.0, s) if s > 0 else max(0.0, -s))
        buzz_line = f"Adanos sentiment score: {s:+.2f} -> *{direction}*\n`{bar}`"
        parts.append(f"*Sentiment (weight 40%)*\n{buzz_line}")

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
        parts.append(f"*Political Activity (weight 35%)*\n" + "\n".join(details))

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
        parts.append(f"*Analyst Consensus (weight 25%)*\n" + "\n".join(details))

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

"""Socket Mode listener for Slack interactive button callbacks.

Handles Approve and Reject button clicks sent from signal alert messages.
Runs as a background daemon thread alongside APScheduler so the main process
stays unblocked.

When a user clicks Approve, this module:
  1. Looks up the pending approval and signal from SQLite.
  2. Re-fetches the current price from the broker.
  3. Calls runner.submit_signal_order with require_approval=False.
  4. Updates the original Slack message to show the order ID.

When a user clicks Reject:
  1. Marks the pending approval as rejected in SQLite.
  2. Updates the original Slack message to show "Rejected".

Typical usage (from main.py):
    from utils.slack_actions import start_socket_mode
    start_socket_mode(broker, settings, notifier)
"""
import threading
from typing import Optional

from loguru import logger
from slack_sdk import WebClient
from slack_sdk.socket_mode import SocketModeClient
from slack_sdk.socket_mode.request import SocketModeRequest
from slack_sdk.socket_mode.response import SocketModeResponse

from brokers.base import Broker
from config.settings import Settings
from utils.notifier import SlackNotifier


def start_socket_mode(broker: Broker, settings: Settings, notifier: SlackNotifier) -> None:
    """Start the Socket Mode listener in a background daemon thread.

    If SLACK_APP_TOKEN is not configured, logs a warning and returns without
    starting the listener. In that case REQUIRE_APPROVAL=true will still send
    Slack notifications, but buttons will not be functional.

    Args:
        broker: Broker instance used to fetch prices and submit orders on approval.
        settings: Application settings loaded from environment variables.
        notifier: SlackNotifier instance used to update the original message.
    """
    if not settings.slack_app_token:
        logger.warning(
            "SLACK_APP_TOKEN not set; Slack interactive buttons are disabled. "
            "Set REQUIRE_APPROVAL=false or configure SLACK_APP_TOKEN to enable buttons."
        )
        return

    def run() -> None:
        client = SocketModeClient(
            app_token=settings.slack_app_token,
            web_client=WebClient(token=settings.slack_bot_token),
        )

        def handle_event(sc: SocketModeClient, req: SocketModeRequest) -> None:
            if req.type != "interactive":
                return
            # Slack requires acknowledgement within 3 seconds before sending a timeout.
            sc.send_socket_mode_response(SocketModeResponse(envelope_id=req.envelope_id))

            payload = req.payload
            actions = payload.get("actions", [])
            for action in actions:
                action_id = action.get("action_id")
                value = action.get("value")
                if not action_id or not value:
                    continue
                try:
                    signal_id = int(value)
                except ValueError:
                    logger.warning(f"Non-integer signal_id in button value: {value!r}")
                    continue

                if action_id == "approve_signal":
                    _handle_approve(signal_id, broker, settings, notifier)
                elif action_id == "reject_signal":
                    _handle_reject(signal_id, settings, notifier)

        client.socket_mode_request_listeners.append(handle_event)
        logger.info("Socket Mode listener started; Approve/Reject buttons are active")
        client.connect()

    thread = threading.Thread(target=run, daemon=True, name="slack-socket-mode")
    thread.start()


def _handle_approve(
    signal_id: int,
    broker: Broker,
    settings: Settings,
    notifier: SlackNotifier,
) -> None:
    """Execute an approved signal: submit the order and update the Slack message.

    Opens its own DB connection so it does not share state with the orchestrator's
    connection, which runs in the main thread.

    Args:
        signal_id: Primary key of the signal in the signals table.
        broker: Broker instance for price fetch and order submission.
        settings: Application settings (used for min_confidence).
        notifier: SlackNotifier for updating the original approval message.
    """
    import db.repository as repo
    from db.models import engine
    from execution import runner

    logger.info(f"Approve clicked for signal_id={signal_id}")

    with engine.connect() as conn:
        pending = repo.get_pending_approval(conn, signal_id)
        if not pending:
            logger.warning(f"No pending approval found for signal_id={signal_id}")
            return
        if pending["status"] != "pending":
            logger.info(
                f"Signal {signal_id} is already {pending['status']}; ignoring duplicate click"
            )
            return

        signal = repo.get_signal_by_id(conn, signal_id)
        if not signal:
            logger.error(f"Signal {signal_id} not found in DB during approval")
            repo.resolve_pending_approval(conn, signal_id, "failed")
            notifier.update_approval_message(pending["notification_metadata"], status="failed")
            return

        try:
            equity = broker.get_account_equity()
            order_id = runner.submit_signal_order(
                signal=signal,
                broker=broker,
                db_conn=conn,
                account_equity=equity,
                min_confidence=0.0,  # already validated when the signal was originally created
                require_approval=False,
                notifier=None,  # suppress a second Slack message; we update the original instead
            )
        except Exception as exc:
            logger.error(f"Order submission failed for approved signal {signal_id}: {exc}")
            repo.resolve_pending_approval(conn, signal_id, "failed")
            notifier.update_approval_message(pending["notification_metadata"], status="failed")
            return

        status = "approved" if order_id else "failed"
        repo.resolve_pending_approval(conn, signal_id, status)
        notifier.update_approval_message(
            pending["notification_metadata"],
            status=status,
            order_id=order_id,
        )
        logger.info(f"Signal {signal_id} approved; order_id={order_id}, status={status}")


def _handle_reject(signal_id: int, settings: Settings, notifier: SlackNotifier) -> None:
    """Mark an approval as rejected and update the Slack message.

    Args:
        signal_id: Primary key of the signal in the signals table.
        settings: Application settings.
        notifier: SlackNotifier for updating the original approval message.
    """
    import db.repository as repo
    from db.models import engine

    logger.info(f"Reject clicked for signal_id={signal_id}")

    with engine.connect() as conn:
        pending = repo.get_pending_approval(conn, signal_id)
        if not pending:
            logger.warning(f"No pending approval found for signal_id={signal_id}")
            return
        if pending["status"] != "pending":
            logger.info(
                f"Signal {signal_id} is already {pending['status']}; ignoring duplicate click"
            )
            return

        repo.resolve_pending_approval(conn, signal_id, "rejected")
        notifier.update_approval_message(pending["notification_metadata"], status="rejected")
        logger.info(f"Signal {signal_id} rejected by user")

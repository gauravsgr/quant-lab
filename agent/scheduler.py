"""APScheduler configuration for the three daily trading cycles.

Configures a BlockingScheduler with three cron-triggered jobs:
    morning_cycle    - Monday through Friday at 10:15 AM (default: America/New_York)
    afternoon_cycle  - Monday through Friday at 3:45 PM
    weekly_audit     - Every Friday at 4:00 PM

The scheduler is created by build_scheduler() and started by start(), which
blocks the calling thread until a KeyboardInterrupt or SystemExit.

Typical usage (from main.py):
    from agent import scheduler as sched_module
    sched_module.start(orchestrator, timezone=settings.agent_timezone)
"""
from apscheduler.schedulers.blocking import BlockingScheduler
from apscheduler.triggers.cron import CronTrigger
from loguru import logger

from agent.orchestrator import Orchestrator


def build_scheduler(orchestrator: Orchestrator, timezone: str = "America/New_York") -> BlockingScheduler:
    """Create and configure the APScheduler instance with all three jobs.

    Jobs:
        morning_cycle    - Mon-Fri 10:15 AM in `timezone`
        afternoon_cycle  - Mon-Fri 3:45 PM in `timezone`
        weekly_audit     - Friday 4:00 PM in `timezone`

    All jobs use coalesce=True so that if the scheduler was offline during the
    scheduled time, only one catch-up run is triggered (not one per missed interval).

    Args:
        orchestrator: Orchestrator instance whose methods are registered as jobs.
        timezone: IANA timezone name for all cron triggers (default America/New_York).

    Returns:
        A configured but not yet started BlockingScheduler.
    """
    scheduler = BlockingScheduler(timezone=timezone)

    scheduler.add_job(
        orchestrator.run_morning_cycle,
        trigger=CronTrigger(day_of_week="mon-fri", hour=10, minute=15, timezone=timezone),
        id="morning_cycle",
        name="Morning signal cycle (10:15 EST)",
        misfire_grace_time=300,  # 5-minute tolerance for late starts
        coalesce=True,
    )

    scheduler.add_job(
        orchestrator.run_afternoon_cycle,
        trigger=CronTrigger(day_of_week="mon-fri", hour=15, minute=45, timezone=timezone),
        id="afternoon_cycle",
        name="Afternoon mark-to-market (3:45 EST)",
        misfire_grace_time=300,
        coalesce=True,
    )

    scheduler.add_job(
        orchestrator.run_weekly_audit,
        trigger=CronTrigger(day_of_week="fri", hour=16, minute=0, timezone=timezone),
        id="weekly_audit",
        name="Weekly precision audit (Fri 4:00 PM EST)",
        misfire_grace_time=900,  # 15-minute tolerance
        coalesce=True,
    )

    return scheduler


def start(orchestrator: Orchestrator, timezone: str = "America/New_York") -> None:
    """Build and start the scheduler, blocking until interrupted.

    Logs the next scheduled run time for each job before starting.
    Catches KeyboardInterrupt and SystemExit to shut down cleanly.

    Args:
        orchestrator: Orchestrator instance to register as job target.
        timezone: IANA timezone name for all cron triggers.
    """
    scheduler = build_scheduler(orchestrator, timezone)

    jobs = scheduler.get_jobs()
    for job in jobs:
        logger.info(f"Scheduled: {job.name} | next run: {job.next_run_time}")

    logger.info("Scheduler started. Press Ctrl+C to stop.")
    try:
        scheduler.start()
    except (KeyboardInterrupt, SystemExit):
        logger.info("Scheduler stopped.")

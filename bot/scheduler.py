import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.cron import CronTrigger
from apscheduler.triggers.interval import IntervalTrigger
import pytz

from bot.config import get_curator_ids, get_today_local
from bot.notifier import Notifier, check_and_alert, check_delayed_responses, send_daily_reminders
from bot.sheets import append_summary
from bot.summarizer import generate_summary
from db import crud

logger = logging.getLogger(__name__)


async def run_daily_summary() -> None:
    today = get_today_local()
    curator_ids = get_curator_ids()
    conversations = await crud.get_active_conversations()

    logger.info("Running daily summary for %d conversations (date=%s)", len(conversations), today)

    skipped_empty = 0
    for conv in conversations:
        try:
            messages = await crud.get_messages_for_day(conv.id, today)
            if not messages:
                skipped_empty += 1
                continue
            summary = await generate_summary(conv, messages, curator_ids, today)
            await append_summary(summary)
            deleted = await crud.delete_messages_for_day(conv.id, today)
            logger.info(
                "Summary done: peer_id=%s msgs=%d deleted=%d",
                conv.vk_peer_id,
                len(messages),
                deleted,
            )
        except Exception:
            logger.exception(
                "Failed to process summary for peer_id=%s — messages kept",
                conv.vk_peer_id,
            )
    if skipped_empty:
        logger.info("Daily summary: skipped %d conversation(s) with no messages today", skipped_empty)


async def run_cleanup() -> None:
    retention = int(os.getenv("MESSAGE_RETENTION_DAYS", "30"))
    deleted = await crud.delete_old_messages(retention)
    logger.info("Cleanup: deleted %d messages older than %d days", deleted, retention)


def _parse_hhmm(value: str, default: str) -> tuple[int, int]:
    raw = value or default
    try:
        h, m = raw.split(":")
        return int(h), int(m)
    except ValueError:
        h, m = default.split(":")
        return int(h), int(m)


def build_scheduler(
    notifier: Notifier | None = None,
    bots: list | None = None,
) -> AsyncIOScheduler:
    tz_name = os.getenv("TIMEZONE", "Europe/Moscow")
    tz = pytz.timezone(tz_name)
    scheduler = AsyncIOScheduler(timezone=tz)

    sh, sm = _parse_hhmm(os.getenv("SUMMARY_TIME", ""), "22:00")
    ch, cm = _parse_hhmm(os.getenv("CLEANUP_TIME", ""), "03:00")
    rh, rm = _parse_hhmm(os.getenv("REMINDER_TIME", ""), "20:00")

    scheduler.add_job(
        run_daily_summary,
        CronTrigger(hour=sh, minute=sm, timezone=tz),
        id="daily_summary",
        replace_existing=True,
    )
    scheduler.add_job(
        run_cleanup,
        CronTrigger(hour=ch, minute=cm, timezone=tz),
        id="cleanup",
        replace_existing=True,
    )

    if bots:
        scheduler.add_job(
            send_daily_reminders,
            CronTrigger(hour=rh, minute=rm, timezone=tz),
            id="daily_reminders",
            replace_existing=True,
            kwargs={"bots": bots},
        )
        logger.info("Daily reminders registered: %02d:%02d %s", rh, rm, tz_name)

    alert_minutes = int(os.getenv("ALERT_CHECK_INTERVAL_MIN", "15"))
    if notifier is not None and alert_minutes > 0:
        scheduler.add_job(
            check_and_alert,
            IntervalTrigger(minutes=alert_minutes, timezone=tz),
            id="alert_check",
            replace_existing=True,
            kwargs={"notifier": notifier, "bots": bots},
        )
        logger.info("Alert checker registered: every %d min", alert_minutes)

    delayed_minutes = int(os.getenv("DELAYED_CHECK_INTERVAL_MIN", "10"))
    if notifier is not None and delayed_minutes > 0:
        scheduler.add_job(
            check_delayed_responses,
            IntervalTrigger(minutes=delayed_minutes, timezone=tz),
            id="delayed_check",
            replace_existing=True,
            kwargs={"notifier": notifier},
        )
        logger.info("Delayed-response checker registered: every %d min", delayed_minutes)

    logger.info(
        "Scheduler configured: summary=%02d:%02d cleanup=%02d:%02d reminder=%02d:%02d tz=%s",
        sh, sm, ch, cm, rh, rm, tz_name,
    )
    return scheduler

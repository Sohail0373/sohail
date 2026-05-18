from __future__ import annotations

import logging

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="UTC")


async def _refresh_all_feeds() -> None:
    """Scheduled job: regenerate feeds for every active store."""
    from .database import SessionLocal
    from .feeds.generator import generate_feeds_for_shop
    from .models import Store

    db = SessionLocal()
    try:
        stores = db.query(Store).filter(Store.is_active.is_(True)).all()
    finally:
        db.close()

    logger.info("Scheduled refresh starting — %d active store(s)", len(stores))
    for store in stores:
        try:
            await generate_feeds_for_shop(store.shop_domain, store.access_token)
        except Exception as exc:
            logger.error("Scheduled refresh failed for %s: %s", store.shop_domain, exc)


def start_scheduler() -> None:
    scheduler.add_job(
        _refresh_all_feeds,
        trigger=IntervalTrigger(hours=settings.FEED_REFRESH_HOURS),
        id="refresh_all_feeds",
        replace_existing=True,
        misfire_grace_time=300,    # allow up to 5 min late execution
    )
    scheduler.start()
    logger.info("APScheduler started — feed refresh every %dh", settings.FEED_REFRESH_HOURS)

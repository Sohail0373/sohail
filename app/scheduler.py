from __future__ import annotations

import logging
import os

from apscheduler.schedulers.asyncio import AsyncIOScheduler
from apscheduler.triggers.interval import IntervalTrigger

from .config import settings

logger = logging.getLogger(__name__)
scheduler = AsyncIOScheduler(timezone="UTC")


async def _refresh_all_feeds() -> None:
    """
    Scheduled job: regenerate feeds for every store that already has
    XML files on disk.  Uses the public (no-auth) generator so no
    tokens or database records are required.
    """
    from .feeds.public_generator import generate_feeds_public

    feeds_dir = settings.FEEDS_DIR
    if not os.path.isdir(feeds_dir):
        logger.info("Scheduled refresh: feeds directory not found, skipping")
        return

    # Discover stores by scanning the feeds directory.
    # Each sub-directory whose name is a store slug that has at least one
    # .xml file is considered an active store.
    slugs = [
        slug
        for slug in os.listdir(feeds_dir)
        if os.path.isdir(os.path.join(feeds_dir, slug))
        and any(
            f.endswith(".xml")
            for f in os.listdir(os.path.join(feeds_dir, slug))
        )
    ]

    if not slugs:
        logger.info("Scheduled refresh: no stores found in %s", feeds_dir)
        return

    logger.info("Scheduled refresh starting — %d store(s): %s",
                len(slugs), ", ".join(slugs))

    for slug in slugs:
        # Reconstruct domain from slug (slug = domain minus .myshopify.com,
        # dots replaced with dashes — reverse that best-effort)
        shop_domain = slug.replace("-", ".") + ".myshopify.com"
        logger.info("Refreshing feeds for %s (slug: %s)", shop_domain, slug)
        try:
            result = await generate_feeds_public(
                shop_domain=shop_domain,
                shop_currency="USD",   # default; feeds store currency in XML anyway
            )
            logger.info(
                "Refresh done for %s — %d products, %d items/feed",
                shop_domain, result["products"], result["items"],
            )
        except Exception as exc:
            logger.error("Scheduled refresh failed for %s: %s",
                         shop_domain, exc, exc_info=True)


def start_scheduler() -> None:
    scheduler.add_job(
        _refresh_all_feeds,
        trigger=IntervalTrigger(hours=settings.FEED_REFRESH_HOURS),
        id="_refresh_all_feeds",
        replace_existing=True,
        misfire_grace_time=600,   # allow up to 10 min late (large stores take time)
    )
    scheduler.start()
    logger.info("APScheduler started — feed refresh every %dh",
                settings.FEED_REFRESH_HOURS)

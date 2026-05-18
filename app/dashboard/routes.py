from __future__ import annotations

import logging
from datetime import timezone

from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Request
from fastapi.responses import HTMLResponse
from fastapi.templating import Jinja2Templates
from sqlalchemy.orm import Session

from ..auth.shopify_oauth import shop_to_slug
from ..config import settings
from ..database import get_db
from ..exchange_rates import FEED_CURRENCIES, FEED_REGIONS
from ..models import Store

router = APIRouter()
logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="app/templates")


@router.get("/dashboard/{shop_domain}", response_class=HTMLResponse, tags=["Dashboard"])
async def dashboard(
    request: Request,
    shop_domain: str,
    db: Session = Depends(get_db),
):
    store = db.query(Store).filter(Store.shop_domain == shop_domain).first()
    if not store:
        raise HTTPException(status_code=404, detail="Store not found. Please install the app first.")

    slug = shop_to_slug(shop_domain)
    feed_urls = {
        region: f"{settings.APP_URL}/feed/{slug}/{region}.xml"
        for region in FEED_CURRENCIES
    }

    # Ensure last_feed_generated is timezone-aware for template comparisons
    last_gen = store.last_feed_generated
    if last_gen and last_gen.tzinfo is None:
        last_gen = last_gen.replace(tzinfo=timezone.utc)

    # Build ordered list for the template: (slug, country_name, currency_code, flag)
    feed_regions = [
        (slug, country, currency, flag)
        for slug, (country, currency, flag) in FEED_REGIONS.items()
    ]

    return templates.TemplateResponse(
        "dashboard.html",
        {
            "request": request,
            "store": store,
            "feed_urls": feed_urls,
            "feed_regions": feed_regions,
            "last_generated": last_gen,
            "app_url": settings.APP_URL,
        },
    )


@router.post("/dashboard/{shop_domain}/refresh", tags=["Dashboard"])
async def manual_refresh(
    shop_domain: str,
    background_tasks: BackgroundTasks,
    db: Session = Depends(get_db),
):
    store = db.query(Store).filter(Store.shop_domain == shop_domain, Store.is_active.is_(True)).first()
    if not store:
        raise HTTPException(status_code=404, detail="Store not found or app not installed")

    background_tasks.add_task(_run_refresh, shop_domain, store.access_token)
    return {"status": "ok", "message": "Feed refresh queued — check back in a minute"}


async def _run_refresh(shop_domain: str, token: str) -> None:
    from ..feeds.generator import generate_feeds_for_shop
    try:
        await generate_feeds_for_shop(shop_domain, token)
    except Exception as exc:
        logger.error("Manual refresh failed for %s: %s", shop_domain, exc)

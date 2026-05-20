from __future__ import annotations

import os
import logging
from datetime import datetime, timezone
from pydantic import BaseModel

from fastapi import APIRouter, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from ..config import settings
from ..exchange_rates import FEED_REGIONS

router = APIRouter()
logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="app/templates")

_VALID_REGIONS = frozenset(FEED_REGIONS.keys())


class GenerateRequest(BaseModel):
    shop_domain: str
    shop_currency: str = "USD"


# ── Main dashboard (no-auth, public) ──────────────────────────────────────────

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


@router.post("/api/generate", tags=["Public API"])
async def api_generate(body: GenerateRequest):
    """Generate feeds for any public Shopify store. No token required."""
    from .public_generator import generate_feeds_public
    try:
        result = await generate_feeds_public(body.shop_domain, body.shop_currency)
        return result
    except RuntimeError as exc:
        raise HTTPException(status_code=400, detail=str(exc))
    except Exception as exc:
        logger.error("Feed generation failed for %s: %s", body.shop_domain, exc, exc_info=True)
        raise HTTPException(status_code=500, detail=f"Generation failed: {exc}")


@router.get("/api/stores", tags=["Public API"])
async def api_stores():
    """List all stores that have generated feeds."""
    stores = []
    feeds_dir = settings.FEEDS_DIR
    if os.path.isdir(feeds_dir):
        for slug in sorted(os.listdir(feeds_dir)):
            slug_path = os.path.join(feeds_dir, slug)
            if not os.path.isdir(slug_path):
                continue
            xml_files = [f for f in os.listdir(slug_path) if f.endswith(".xml")]
            if not xml_files:
                continue
            # Get latest modification time
            latest = max(
                os.path.getmtime(os.path.join(slug_path, f)) for f in xml_files
            )
            updated_dt = datetime.fromtimestamp(latest, tz=timezone.utc)
            stores.append({
                "slug": slug,
                "feed_count": len(xml_files),
                "updated": updated_dt.strftime("%d %b %Y, %H:%M UTC"),
            })
    return {"stores": stores}


@router.get("/api/store/{slug}", tags=["Public API"])
async def api_store(slug: str):
    """Get feed URLs for a previously generated store."""
    slug_path = os.path.join(settings.FEEDS_DIR, slug)
    if not os.path.isdir(slug_path):
        raise HTTPException(status_code=404, detail="Store not found")
    feed_urls = {
        region: f"{settings.APP_URL}/feed/{slug}/{region}.xml"
        for region in FEED_REGIONS
    }
    return {
        "slug": slug,
        "shop_domain": slug + ".myshopify.com",
        "shop_name": slug,
        "products": 0,
        "items": 0,
        "feed_urls": feed_urls,
    }


@router.get(
    "/feed/{shop_slug}/{region}.xml",
    response_class=FileResponse,
    summary="Serve a cached XML product feed",
    tags=["Feeds"],
)
async def get_feed(shop_slug: str, region: str):
    if region not in _VALID_REGIONS:
        raise HTTPException(status_code=404, detail=f"Unknown region '{region}'. Valid: {sorted(_VALID_REGIONS)}")

    # Prevent path traversal
    safe_slug = os.path.basename(shop_slug)
    if not safe_slug or safe_slug != shop_slug:
        raise HTTPException(status_code=400, detail="Invalid shop slug")

    path = os.path.join(settings.FEEDS_DIR, safe_slug, f"{region}.xml")

    if not os.path.isfile(path):
        raise HTTPException(
            status_code=404,
            detail="Feed not yet generated. Please wait a few minutes after install, or trigger a manual refresh.",
        )

    return FileResponse(
        path,
        media_type="application/rss+xml; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )

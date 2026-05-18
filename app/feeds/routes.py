from __future__ import annotations

import os

from fastapi import APIRouter, HTTPException
from fastapi.responses import FileResponse

from ..config import settings
from ..exchange_rates import FEED_REGIONS

router = APIRouter()

_VALID_REGIONS = frozenset(FEED_REGIONS.keys())


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

from __future__ import annotations

import asyncio
import os
import uuid
import logging
from datetime import datetime, timezone
from pydantic import BaseModel

from fastapi import APIRouter, BackgroundTasks, HTTPException, Request
from fastapi.responses import FileResponse, HTMLResponse
from fastapi.templating import Jinja2Templates

from ..config import settings
from ..exchange_rates import FEED_REGIONS

router = APIRouter()
logger = logging.getLogger(__name__)
templates = Jinja2Templates(directory="app/templates")

_VALID_REGIONS = frozenset(FEED_REGIONS.keys())

# ── In-memory job store ────────────────────────────────────────────────────────
# Keeps the last 200 jobs; older ones are dropped to avoid unbounded growth.

_jobs: dict[str, dict] = {}
_MAX_JOBS = 200


def _new_job() -> dict:
    return {
        "status":         "queued",    # queued | running | done | error
        "phase":          "queued",    # queued | fetching | rates | generating | done
        "page":           0,
        "products":       0,
        "region_num":     0,
        "total_regions":  len(FEED_REGIONS),
        "message":        "Queued — starting shortly…",
        "feed_urls":      None,
        "items":          0,
        "error":          None,
        "started":        datetime.now(timezone.utc).isoformat(),
        "finished":       None,
    }


def _update_job(job_id: str, update: dict) -> None:
    if job_id in _jobs:
        _jobs[job_id].update(update)


def _prune_jobs() -> None:
    if len(_jobs) > _MAX_JOBS:
        oldest = sorted(_jobs.keys())[: len(_jobs) - _MAX_JOBS]
        for k in oldest:
            _jobs.pop(k, None)


# ── Models ─────────────────────────────────────────────────────────────────────

class GenerateRequest(BaseModel):
    shop_domain:   str
    shop_currency: str = "USD"


# ── Dashboard ──────────────────────────────────────────────────────────────────

@router.get("/", response_class=HTMLResponse, include_in_schema=False)
async def index(request: Request):
    return templates.TemplateResponse("index.html", {"request": request})


# ── Generate endpoint (returns immediately with job_id) ───────────────────────

@router.post("/api/generate", tags=["Public API"])
async def api_generate(body: GenerateRequest, background_tasks: BackgroundTasks):
    """
    Start feed generation as a background job.
    Returns a job_id immediately — poll /api/jobs/{job_id} for progress.
    """
    _prune_jobs()
    job_id = str(uuid.uuid4())
    _jobs[job_id] = _new_job()
    background_tasks.add_task(_run_generate, job_id, body.shop_domain, body.shop_currency)
    return {"job_id": job_id, "status": "queued"}


async def _run_generate(job_id: str, shop_domain: str, shop_currency: str) -> None:
    """Background task: runs the full fetch → generate pipeline."""
    from .public_generator import generate_feeds_public

    _update_job(job_id, {"status": "running", "phase": "fetching"})

    def progress_cb(update: dict) -> None:
        phase = update.get("phase", "")
        patch: dict = {"message": update.get("message", "")}

        if phase == "fetching":
            patch.update({
                "phase":    "fetching",
                "status":   "running",
                "page":     update.get("page", 0),
                "products": update.get("products", 0),
            })
        elif phase == "rates":
            patch.update({
                "phase":    "rates",
                "products": update.get("products", 0),
            })
        elif phase == "generating":
            patch.update({
                "phase":          "generating",
                "region_num":     update.get("region_num", 0),
                "total_regions":  update.get("total_regions", len(FEED_REGIONS)),
                "products":       update.get("products", 0),
            })
        elif phase == "done":
            patch.update({
                "phase":     "done",
                "status":    "done",
                "products":  update.get("products", 0),
                "items":     update.get("items", 0),
                "feed_urls": update.get("feed_urls"),
                "finished":  datetime.now(timezone.utc).isoformat(),
            })

        _update_job(job_id, patch)

    try:
        result = await generate_feeds_public(
            shop_domain, shop_currency, progress_cb=progress_cb
        )
        _update_job(job_id, {
            "status":    "done",
            "phase":     "done",
            "products":  result["products"],
            "items":     result["items"],
            "feed_urls": result["feed_urls"],
            "finished":  datetime.now(timezone.utc).isoformat(),
            "message":   f"Done! {result['products']:,} products → {result['items']:,} feed items across {result['feed_count']} regions.",
        })
    except RuntimeError as exc:
        logger.warning("Feed generation error for %s: %s", shop_domain, exc)
        _update_job(job_id, {
            "status":   "error",
            "phase":    "error",
            "error":    str(exc),
            "message":  str(exc),
            "finished": datetime.now(timezone.utc).isoformat(),
        })
    except Exception as exc:
        logger.error("Unexpected error for %s: %s", shop_domain, exc, exc_info=True)
        _update_job(job_id, {
            "status":   "error",
            "phase":    "error",
            "error":    f"Unexpected error: {exc}",
            "message":  f"Unexpected error: {exc}",
            "finished": datetime.now(timezone.utc).isoformat(),
        })


# ── Job status polling ─────────────────────────────────────────────────────────

@router.get("/api/jobs/{job_id}", tags=["Public API"])
async def api_job_status(job_id: str):
    """Poll for background job progress. Returns full status dict."""
    if job_id not in _jobs:
        raise HTTPException(status_code=404, detail="Job not found")
    return _jobs[job_id]


# ── Store listing / lookup ─────────────────────────────────────────────────────

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
            latest = max(
                os.path.getmtime(os.path.join(slug_path, f)) for f in xml_files
            )
            updated_dt = datetime.fromtimestamp(latest, tz=timezone.utc)
            stores.append({
                "slug":       slug,
                "feed_count": len(xml_files),
                "updated":    updated_dt.strftime("%d %b %Y, %H:%M UTC"),
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
        "slug":        slug,
        "shop_domain": slug + ".myshopify.com",
        "shop_name":   slug,
        "products":    0,
        "items":       0,
        "feed_urls":   feed_urls,
    }


# ── Feed file serving ──────────────────────────────────────────────────────────

@router.get(
    "/feed/{shop_slug}/{region}.xml",
    response_class=FileResponse,
    summary="Serve a cached XML product feed",
    tags=["Feeds"],
)
async def get_feed(shop_slug: str, region: str):
    if region not in _VALID_REGIONS:
        raise HTTPException(
            status_code=404,
            detail=f"Unknown region '{region}'. Valid: {sorted(_VALID_REGIONS)}",
        )
    safe_slug = os.path.basename(shop_slug)
    if not safe_slug or safe_slug != shop_slug:
        raise HTTPException(status_code=400, detail="Invalid shop slug")

    path = os.path.join(settings.FEEDS_DIR, safe_slug, f"{region}.xml")
    if not os.path.isfile(path):
        raise HTTPException(
            status_code=404,
            detail=(
                "Feed not yet generated. Please wait a few minutes after install, "
                "or trigger a manual refresh."
            ),
        )
    return FileResponse(
        path,
        media_type="application/rss+xml; charset=utf-8",
        headers={
            "Cache-Control": "public, max-age=3600",
            "X-Content-Type-Options": "nosniff",
        },
    )

from __future__ import annotations

import logging
from contextlib import asynccontextmanager

from fastapi import FastAPI
from fastapi.middleware.cors import CORSMiddleware
from fastapi.responses import JSONResponse

from .config import settings
from .database import init_db
from .scheduler import scheduler, start_scheduler

logging.basicConfig(
    level=getattr(logging, settings.LOG_LEVEL.upper(), logging.INFO),
    format="%(asctime)s [%(levelname)-8s] %(name)s: %(message)s",
)
logger = logging.getLogger(__name__)


@asynccontextmanager
async def lifespan(app: FastAPI):
    import os
    try:
        init_db()
        logger.info("Database tables initialised OK")
    except Exception as exc:
        logger.critical("STARTUP FAILED — could not initialise database: %s", exc, exc_info=True)
        raise
    start_scheduler()
    logger.info("%s is ready", settings.APP_NAME)
    raw_key = os.environ.get("SHOPIFY_API_KEY", "NOT_IN_ENV")
    logger.info("RAW ENV SHOPIFY_API_KEY: %s", raw_key[:8] if raw_key != "NOT_IN_ENV" else "NOT_IN_ENV")
    logger.info("RAW ENV APP_URL: %s", os.environ.get("APP_URL", "NOT_IN_ENV"))
    yield
    scheduler.shutdown(wait=False)
    logger.info("%s shut down", settings.APP_NAME)


app = FastAPI(
    title=settings.APP_NAME,
    description=(
        "Multi-tenant Shopify app that generates Pinterest Catalog and "
        "Google Merchant Center-compatible XML product feeds for any store."
    ),
    version="1.0.0",
    lifespan=lifespan,
    docs_url="/docs" if settings.DEBUG else None,
    redoc_url="/redoc" if settings.DEBUG else None,
)

app.add_middleware(
    CORSMiddleware,
    allow_origins=["*"],
    allow_methods=["GET", "POST"],
    allow_headers=["*"],
)

# ── Routers ───────────────────────────────────────────────────────────────────
from .auth.routes import router as auth_router          # noqa: E402
from .feeds.routes import router as feeds_router        # noqa: E402
from .dashboard.routes import router as dashboard_router  # noqa: E402
from .webhooks.routes import router as webhooks_router  # noqa: E402

app.include_router(auth_router)
app.include_router(feeds_router)
app.include_router(dashboard_router)
app.include_router(webhooks_router)


# ── Utility endpoints ──────────────────────────────────────────────────────────

@app.get("/health", tags=["System"])
async def health():
    return {"status": "ok", "app": settings.APP_NAME}


@app.get("/", tags=["System"])
async def root():
    return JSONResponse({
        "name": settings.APP_NAME,
        "docs": f"{settings.APP_URL}/docs" if settings.DEBUG else "disabled in production",
        "install": f"{settings.APP_URL}/install?shop=yourstore.myshopify.com",
    })

from __future__ import annotations

import logging

from fastapi import APIRouter, Header, HTTPException, Request

from ..auth.shopify_oauth import verify_webhook_hmac
from ..database import SessionLocal
from ..models import Store

router = APIRouter(prefix="/webhooks", tags=["Webhooks"])
logger = logging.getLogger(__name__)


@router.post("/app-uninstalled", summary="Shopify app/uninstalled webhook")
async def app_uninstalled(
    request: Request,
    x_shopify_shop_domain: str = Header(...),
    x_shopify_hmac_sha256: str = Header(...),
):
    body = await request.body()

    if not verify_webhook_hmac(body, x_shopify_hmac_sha256):
        raise HTTPException(status_code=401, detail="Webhook HMAC verification failed")

    shop = x_shopify_shop_domain.lower().strip()
    db = SessionLocal()
    try:
        store = db.query(Store).filter(Store.shop_domain == shop).first()
        if store:
            store.is_active = False
            store.access_token = ""     # revoke locally — Shopify has already revoked server-side
            db.commit()
            logger.info("Store uninstalled and deactivated: %s", shop)
        else:
            logger.warning("Uninstall webhook for unknown shop: %s", shop)
    finally:
        db.close()

    # Shopify expects a 200 response within 5 s
    return {"status": "ok"}

from __future__ import annotations

import logging

import httpx
from fastapi import APIRouter, BackgroundTasks, Depends, HTTPException, Query, Request
from fastapi.responses import RedirectResponse
from itsdangerous import BadSignature, SignatureExpired, URLSafeTimedSerializer
from sqlalchemy.orm import Session

from ..config import settings
from ..database import get_db
from ..models import Store
from .shopify_oauth import build_oauth_url, sanitize_shop, verify_oauth_hmac

router = APIRouter()
logger = logging.getLogger(__name__)
_signer = URLSafeTimedSerializer(settings.SECRET_KEY)


@router.get("/install", summary="Install entry-point — redirects merchant to Shopify OAuth")
async def install(shop: str = Query(..., description="e.g. mystore.myshopify.com")):
    try:
        shop = sanitize_shop(shop)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    # Embed the shop domain in a signed, time-limited state token to prevent CSRF
    state = _signer.dumps({"shop": shop}, salt="oauth-state")
    return RedirectResponse(build_oauth_url(shop, state))


@router.get("/auth/callback", summary="Shopify OAuth callback")
async def auth_callback(
    request: Request,
    background_tasks: BackgroundTasks,
    code: str = Query(...),
    shop: str = Query(...),
    state: str = Query(...),
    db: Session = Depends(get_db),
):
    # 1. Verify Shopify HMAC
    if not verify_oauth_hmac(dict(request.query_params)):
        raise HTTPException(status_code=400, detail="HMAC verification failed")

    # 2. Verify signed state (prevents CSRF; expires after 10 min)
    try:
        payload = _signer.loads(state, salt="oauth-state", max_age=600)
    except SignatureExpired:
        raise HTTPException(status_code=400, detail="State token expired — please reinstall")
    except BadSignature:
        raise HTTPException(status_code=400, detail="Invalid state token")

    try:
        shop = sanitize_shop(shop)
    except ValueError as exc:
        raise HTTPException(status_code=400, detail=str(exc))

    if payload.get("shop") != shop:
        raise HTTPException(status_code=400, detail="Shop domain mismatch in state")

    # 3. Exchange authorisation code for a permanent access token
    try:
        async with httpx.AsyncClient(timeout=30.0) as client:
            resp = await client.post(
                f"https://{shop}/admin/oauth/access_token",
                json={
                    "client_id": settings.SHOPIFY_API_KEY,
                    "client_secret": settings.SHOPIFY_API_SECRET,
                    "code": code,
                },
            )
            resp.raise_for_status()
            token_data = resp.json()
    except httpx.HTTPError as exc:
        logger.error("Token exchange failed for %s: %s", shop, exc)
        raise HTTPException(status_code=502, detail="Failed to obtain access token from Shopify")

    access_token = token_data.get("access_token")
    if not access_token:
        raise HTTPException(status_code=502, detail="Empty access token received from Shopify")

    # 4. Upsert the store record
    store = db.query(Store).filter(Store.shop_domain == shop).first()
    if store:
        store.access_token = access_token
        store.is_active = True
        logger.info("Reinstalled: %s", shop)
    else:
        store = Store(shop_domain=shop, access_token=access_token)
        db.add(store)
        logger.info("New install: %s", shop)
    db.commit()

    # 5. Post-install tasks run asynchronously so the redirect is instant
    background_tasks.add_task(_post_install, shop, access_token)

    return RedirectResponse(f"/dashboard/{shop}")


async def _post_install(shop: str, token: str) -> None:
    """Register the uninstall webhook then generate the first batch of feeds."""
    await _register_uninstall_webhook(shop, token)
    from ..feeds.generator import generate_feeds_for_shop
    await generate_feeds_for_shop(shop, token)


async def _register_uninstall_webhook(shop: str, token: str) -> None:
    url = f"https://{shop}/admin/api/{settings.SHOPIFY_API_VERSION}/webhooks.json"
    payload = {
        "webhook": {
            "topic": "app/uninstalled",
            "address": f"{settings.APP_URL}/webhooks/app-uninstalled",
            "format": "json",
        }
    }
    try:
        async with httpx.AsyncClient(timeout=20.0) as client:
            resp = await client.post(
                url,
                json=payload,
                headers={"X-Shopify-Access-Token": token},
            )
        # 422 means the webhook already exists — that's fine
        if resp.status_code not in (200, 201, 422):
            logger.warning("Webhook registration returned %s for %s", resp.status_code, shop)
        else:
            logger.info("Uninstall webhook registered for %s", shop)
    except httpx.HTTPError as exc:
        logger.error("Webhook registration failed for %s: %s", shop, exc)

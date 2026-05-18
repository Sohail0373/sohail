from __future__ import annotations

import hashlib
import hmac as _hmac
import urllib.parse

from ..config import settings


def verify_oauth_hmac(params: dict[str, str]) -> bool:
    """Verify the HMAC signature sent by Shopify on the OAuth callback URL."""
    received = params.get("hmac", "")
    # Build the canonical message: sort all params except hmac, join as key=value&...
    message = "&".join(
        f"{k}={v}"
        for k, v in sorted(params.items())
        if k != "hmac"
    )
    expected = _hmac.new(
        settings.SHOPIFY_API_SECRET.encode(),
        message.encode(),
        hashlib.sha256,
    ).hexdigest()
    return _hmac.compare_digest(expected, received)


def verify_webhook_hmac(body: bytes, b64_signature: str) -> bool:
    """Verify the HMAC-SHA256 signature on incoming Shopify webhook requests."""
    import base64
    digest = _hmac.new(
        settings.SHOPIFY_API_SECRET.encode(),
        body,
        hashlib.sha256,
    ).digest()
    return _hmac.compare_digest(base64.b64encode(digest).decode(), b64_signature)


def build_oauth_url(shop: str, state: str) -> str:
    """Return the Shopify OAuth authorization URL for the given shop."""
    params = urllib.parse.urlencode({
        "client_id": settings.SHOPIFY_API_KEY,
        "scope": settings.SHOPIFY_SCOPES,
        "redirect_uri": f"{settings.APP_URL}/auth/callback",
        "state": state,
    })
    return f"https://{shop}/admin/oauth/authorize?{params}"


def sanitize_shop(shop: str) -> str:
    """Validate and normalise a Shopify store domain.

    Raises ValueError if the domain is not a valid *.myshopify.com hostname.
    """
    shop = shop.strip().lower()
    if not shop.endswith(".myshopify.com"):
        raise ValueError(f"Shop must end with .myshopify.com, got: {shop!r}")
    name = shop.removesuffix(".myshopify.com")
    if not name or not all(c.isalnum() or c == "-" for c in name):
        raise ValueError(f"Invalid shop name segment: {name!r}")
    return shop


def shop_to_slug(shop_domain: str) -> str:
    """Convert a shop domain to a URL-safe slug used in feed paths.

    e.g. "my-store.myshopify.com" → "my-store"
    """
    return shop_domain.removesuffix(".myshopify.com").replace(".", "-")

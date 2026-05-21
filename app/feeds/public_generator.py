"""
Public feed generator — no Shopify app or OAuth required.
Uses Shopify's public products.json endpoint (available on all live stores).
"""
from __future__ import annotations

import logging
import os
import re
import tempfile
from typing import AsyncIterator

import httpx
from lxml import etree

from ..auth.shopify_oauth import shop_to_slug
from ..config import settings
from ..exchange_rates import FEED_REGIONS, FALLBACK_RATES, get_exchange_rates

logger = logging.getLogger(__name__)

G_NS = "http://base.google.com/ns/1.0"
G    = f"{{{G_NS}}}"
_HTML_TAG_RE   = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


def _strip_html(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", text)).strip()


def _convert_price(amount: float, from_curr: str, to_curr: str, rates: dict) -> str:
    if from_curr == to_curr:
        return f"{amount:.2f} {to_curr}"
    usd       = amount / rates.get(from_curr, 1.0)
    converted = usd * rates.get(to_curr, FALLBACK_RATES.get(to_curr, 1.0))
    return f"{converted:.2f} {to_curr}"


def _atomic_write(path: str, data: bytes) -> None:
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


async def _fetch_shop_name(shop_domain: str, client: httpx.AsyncClient) -> str:
    """Best-effort: grab shop name from storefront HTML."""
    try:
        r = await client.get(f"https://{shop_domain}/", timeout=10)
        m = re.search(
            r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
            r.text, re.I,
        )
        if m:
            return m.group(1).strip()
        m2 = re.search(r'<title>([^<]+)</title>', r.text, re.I)
        if m2:
            return m2.group(1).strip().split("|")[0].strip()
    except Exception:
        pass
    return shop_domain


async def fetch_products_public(shop_domain: str) -> tuple[list[dict], str]:
    """
    Fetch all active products via public products.json (no auth).
    Returns (products_list, shop_name).
    Primary: cursor-based pagination via Link header (Shopify recommended).
    Fallback: page-based pagination if Link header is absent but batch is full.
    Handles Shopify bot-detection (403/429/430) with exponential back-off + retry.
    """
    import asyncio
    import random

    # Realistic browser User-Agent to avoid Shopify bot detection
    headers = {
        "User-Agent": (
            "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/124.0.0.0 Safari/537.36"
        ),
        "Accept": "application/json, text/plain, */*",
        "Accept-Language": "en-US,en;q=0.9",
    }
    products: list[dict] = []

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0),
        headers=headers,
        follow_redirects=True,
    ) as client:
        shop_name = await _fetch_shop_name(shop_domain, client)

        next_url: str | None = f"https://{shop_domain}/products.json?limit=250"
        page = 0
        retries = 0
        MAX_RETRIES = 5

        while next_url:
            page += 1
            logger.info("[%s] fetching page %d (total so far: %d)",
                        shop_domain, page, len(products))

            try:
                r = await client.get(next_url)

                # ── Rate-limit / bot-detection: back off and retry ─────────────
                if r.status_code in (403, 429, 430):
                    retries += 1
                    if retries > MAX_RETRIES:
                        logger.error(
                            "[%s] blocked after %d retries on page %d — returning %d products fetched so far",
                            shop_domain, MAX_RETRIES, page, len(products),
                        )
                        break
                    # Exponential back-off: 15s, 30s, 60s, 120s, 240s
                    wait = 15 * (2 ** (retries - 1)) + random.uniform(0, 5)
                    logger.warning(
                        "[%s] HTTP %d on page %d (retry %d/%d) — sleeping %.0fs",
                        shop_domain, r.status_code, page, retries, MAX_RETRIES, wait,
                    )
                    await asyncio.sleep(wait)
                    page -= 1   # don't count this as a successful page
                    continue

                retries = 0     # reset on success
                r.raise_for_status()

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                # 401/403 on page 1 almost certainly means password-protected
                if status in (401, 403) and page == 1:
                    raise RuntimeError(
                        f"Shopify returned {status} for {shop_domain}. "
                        "Store may be password-protected or the domain is wrong."
                    ) from exc
                # Otherwise log and stop — return whatever we have
                logger.error("[%s] HTTP %d on page %d — stopping early with %d products",
                             shop_domain, status, page, len(products))
                break

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                retries += 1
                if retries > MAX_RETRIES:
                    logger.error("[%s] too many network errors — stopping with %d products",
                                 shop_domain, len(products))
                    break
                wait = 5 * retries
                logger.warning("[%s] network error (retry %d/%d): %s — sleeping %ds",
                               shop_domain, retries, MAX_RETRIES, exc, wait)
                await asyncio.sleep(wait)
                page -= 1
                continue

            batch = r.json().get("products", [])
            if not batch:
                logger.info("[%s] empty batch on page %d — done", shop_domain, page)
                break
            products.extend(batch)

            # ── Cursor-based next page (Shopify preferred method) ──────────────
            next_url = None
            link_header = r.headers.get("Link", "")
            if link_header:
                for part in link_header.split(","):
                    part = part.strip()
                    if 'rel="next"' in part:
                        m = re.search(r'<([^>]+)>', part)
                        if m:
                            next_url = m.group(1)
                            break

            # ── Fallback: page param when Link header absent but batch is full ─
            if not next_url and len(batch) == 250:
                next_url = (
                    f"https://{shop_domain}/products.json?limit=250&page={page + 1}"
                )
                logger.debug("[%s] no Link header — using page fallback: page %d",
                             shop_domain, page + 1)

            # Polite delay between pages (randomised to avoid bot fingerprint)
            if next_url:
                await asyncio.sleep(random.uniform(0.5, 1.2))

    logger.info("[%s] fetched %d products total (%d pages)",
                shop_domain, len(products), page)
    return products, shop_name


def _build_feed_public(
    shop_domain:   str,
    shop_name:     str,
    shop_url:      str,
    products:      list[dict],
    currency:      str,
    shop_currency: str,
    rates:         dict[str, float],
) -> tuple[bytes, int, int]:
    nsmap   = {"g": G_NS}
    rss     = etree.Element("rss", nsmap=nsmap)
    rss.set("version", "2.0")
    channel = etree.SubElement(rss, "channel")
    etree.SubElement(channel, "title").text       = f"{shop_name} — Product Feed"
    etree.SubElement(channel, "link").text        = shop_url
    etree.SubElement(channel, "description").text = (
        f"Google Shopping / Pinterest Catalog feed — {currency}"
    )

    seen: set[str] = set()
    included = skipped = 0

    for product in products:
        p_id         = str(product.get("id", ""))
        title        = (product.get("title") or "").strip()
        if not title:
            skipped += 1
            continue

        description  = _strip_html(product.get("body_html") or title)[:5000]
        handle       = product.get("handle", "")
        product_url  = f"{shop_url}/products/{handle}"
        product_type = (product.get("product_type") or "").strip()
        vendor       = (product.get("vendor") or "").strip()
        tags_raw     = product.get("tags") or ""
        tags: list[str] = (
            [t.strip() for t in tags_raw.split(",") if t.strip()]
            if isinstance(tags_raw, str) else list(tags_raw)
        )

        product_images: list[str] = [
            img["src"] for img in (product.get("images") or []) if img.get("src")
        ]

        for variant in (product.get("variants") or []):
            v_id = str(variant.get("id", ""))
            if v_id in seen:
                continue
            seen.add(v_id)

            available        = variant.get("available", False)
            availability_str = "in stock" if available else "out of stock"

            try:
                raw_price = float(variant.get("price") or 0)
            except (TypeError, ValueError):
                raw_price = 0.0
            if raw_price <= 0:
                skipped += 1
                continue

            v_img_id  = variant.get("image_id")
            v_img_url = None
            if v_img_id:
                for img in (product.get("images") or []):
                    if img.get("id") == v_img_id:
                        v_img_url = img.get("src")
                        break
            all_images = list(dict.fromkeys(filter(None, [v_img_url] + product_images)))
            if not all_images:
                skipped += 1
                continue

            v_title    = (variant.get("title") or "").strip()
            item_title = (
                f"{title} — {v_title}"
                if v_title and v_title.lower() != "default title"
                else title
            )[:150]

            price_str = _convert_price(raw_price, shop_currency, currency, rates)
            item_url  = f"{product_url}?variant={v_id}"

            item = etree.SubElement(channel, "item")
            etree.SubElement(item, f"{G}id").text               = v_id
            etree.SubElement(item, "title").text                = item_title
            etree.SubElement(item, "description").text          = description
            etree.SubElement(item, "link").text                 = item_url
            etree.SubElement(item, f"{G}image_link").text       = all_images[0]
            for extra in all_images[1:6]:
                etree.SubElement(item, f"{G}additional_image_link").text = extra
            etree.SubElement(item, f"{G}availability").text     = availability_str
            etree.SubElement(item, f"{G}price").text            = price_str
            etree.SubElement(item, f"{G}condition").text        = "new"
            if vendor:
                etree.SubElement(item, f"{G}brand").text        = vendor
            if product_type:
                etree.SubElement(item, f"{G}product_type").text = product_type
            etree.SubElement(item, f"{G}item_group_id").text    = p_id
            sku = (variant.get("sku") or "").strip()
            if sku:
                etree.SubElement(item, f"{G}mpn").text          = sku
            if tags:
                etree.SubElement(item, f"{G}custom_label_0").text = ", ".join(tags[:10])
            included += 1

    xml_bytes = etree.tostring(
        rss, xml_declaration=True, encoding="UTF-8", pretty_print=True
    )
    return xml_bytes, included, skipped


async def generate_feeds_public(
    shop_domain:   str,
    shop_currency: str = "USD",
) -> dict:
    """
    Full pipeline: fetch → generate → write XML files.
    Returns a result dict with stats and feed URLs.
    """
    shop_domain = shop_domain.strip().lower()
    # sanitise
    shop_domain = shop_domain.replace("https://", "").replace("http://", "").split("/")[0]
    if ".myshopify.com" in shop_domain:
        shop_domain = shop_domain.split(".myshopify.com")[0] + ".myshopify.com"
    elif not shop_domain.endswith(".myshopify.com"):
        shop_domain += ".myshopify.com"

    slug      = shop_to_slug(shop_domain)
    shop_url  = f"https://{shop_domain}"

    # Fetch
    products, shop_name = await fetch_products_public(shop_domain)
    if not products:
        raise RuntimeError("No products found. Store may be password-protected.")

    # Exchange rates
    rates = await get_exchange_rates(base="USD", ttl=3600)

    # Generate + write
    total_included = 0
    for region, (country, currency, _flag) in FEED_REGIONS.items():
        xml_bytes, included, _skipped = _build_feed_public(
            shop_domain, shop_name, shop_url,
            products, currency, shop_currency, rates,
        )
        path = os.path.join(settings.FEEDS_DIR, slug, f"{region}.xml")
        _atomic_write(path, xml_bytes)
        total_included = included

    feed_urls = {
        region: f"{settings.APP_URL}/feed/{slug}/{region}.xml"
        for region in FEED_REGIONS
    }

    logger.info(
        "[%s] public feed generation complete: %d products, %d items/feed",
        shop_domain, len(products), total_included,
    )

    return {
        "shop_domain":  shop_domain,
        "shop_name":    shop_name,
        "slug":         slug,
        "products":     len(products),
        "items":        total_included,
        "feed_count":   len(FEED_REGIONS),
        "feed_urls":    feed_urls,
    }

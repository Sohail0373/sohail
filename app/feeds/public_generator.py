"""
Public feed generator — no Shopify app or OAuth required.
Uses Shopify's public products.json endpoint (available on all live stores).

Designed for large catalogues (50k–70k+ products):
  - Streaming XML via lxml.etree.xmlfile — no giant in-memory tree
  - Cursor-based pagination with page-param fallback
  - Exponential back-off on Shopify bot-detection (403/429/430)
  - Progress callback for real-time job status updates
"""
from __future__ import annotations

import asyncio
import logging
import os
import random
import re
import tempfile
from typing import Callable

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

ProgressCb = Callable[[dict], None]

# Realistic browser headers to avoid Shopify bot-detection
_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}


# ── Helpers ────────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", text)).strip()


def _convert_price(amount: float, from_curr: str, to_curr: str, rates: dict) -> str:
    if from_curr == to_curr:
        return f"{amount:.2f} {to_curr}"
    usd       = amount / rates.get(from_curr, 1.0)
    converted = usd * rates.get(to_curr, FALLBACK_RATES.get(to_curr, 1.0))
    return f"{converted:.2f} {to_curr}"


def _noop_cb(_: dict) -> None:
    pass


# ── Shopify product fetcher ────────────────────────────────────────────────────

async def fetch_products_public(
    shop_domain: str,
    progress_cb: ProgressCb = _noop_cb,
) -> tuple[list[dict], str]:
    """
    Fetch ALL products via Shopify's public products.json (no auth).

    Handles stores with 50k–70k+ products:
      - Cursor-based pagination (Link header) as primary method
      - Page-param fallback when Link header absent but batch is full
      - Exponential back-off (15s→30s→60s→120s→240s) on 403/429/430
      - Randomised inter-page delays to avoid bot fingerprint
    """
    products: list[dict] = []

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0),
        headers=_HEADERS,
        follow_redirects=True,
    ) as client:
        # Best-effort: grab store name from storefront HTML
        shop_name = shop_domain
        try:
            r = await client.get(f"https://{shop_domain}/", timeout=10)
            m = re.search(
                r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']',
                r.text, re.I,
            )
            if m:
                shop_name = m.group(1).strip()
            else:
                m2 = re.search(r'<title>([^<]+)</title>', r.text, re.I)
                if m2:
                    shop_name = m2.group(1).strip().split("|")[0].strip()
        except Exception:
            pass

        next_url: str | None = f"https://{shop_domain}/products.json?limit=250"
        page          = 0
        retries       = 0
        MAX_RETRIES   = 6

        while next_url:
            page += 1

            progress_cb({
                "phase":    "fetching",
                "page":     page,
                "products": len(products),
                "message":  f"Fetching page {page} — {len(products):,} products so far…",
            })
            logger.info("[%s] page %d — %d products fetched so far",
                        shop_domain, page, len(products))

            try:
                r = await client.get(next_url)

                # ── Bot-detection / rate-limit: back off and retry ─────────────
                if r.status_code in (403, 429, 430):
                    retries += 1
                    if retries > MAX_RETRIES:
                        logger.error(
                            "[%s] blocked after %d retries on page %d — "
                            "returning %d products fetched so far",
                            shop_domain, MAX_RETRIES, page, len(products),
                        )
                        break
                    wait = 15 * (2 ** (retries - 1)) + random.uniform(0, 8)
                    logger.warning(
                        "[%s] HTTP %d page %d (retry %d/%d) — sleeping %.0fs",
                        shop_domain, r.status_code, page, retries, MAX_RETRIES, wait,
                    )
                    progress_cb({
                        "phase":    "fetching",
                        "page":     page,
                        "products": len(products),
                        "message":  (
                            f"Shopify rate-limited us — waiting {wait:.0f}s before retry "
                            f"(attempt {retries}/{MAX_RETRIES})…"
                        ),
                    })
                    await asyncio.sleep(wait)
                    page -= 1   # don't count this as a successful page
                    continue

                retries = 0     # reset on clean response
                r.raise_for_status()

            except httpx.HTTPStatusError as exc:
                status = exc.response.status_code
                if status in (401, 403) and page == 1:
                    raise RuntimeError(
                        f"Shopify returned {status} for {shop_domain}. "
                        "Store may be password-protected or the domain is wrong."
                    ) from exc
                logger.error("[%s] HTTP %d on page %d — stopping with %d products",
                             shop_domain, status, page, len(products))
                break

            except (httpx.TimeoutException, httpx.NetworkError) as exc:
                retries += 1
                if retries > MAX_RETRIES:
                    logger.error("[%s] too many network errors — stopping with %d products",
                                 shop_domain, len(products))
                    break
                wait = 8 * retries + random.uniform(0, 4)
                logger.warning("[%s] network error retry %d/%d: %s — %.0fs",
                               shop_domain, retries, MAX_RETRIES, exc, wait)
                await asyncio.sleep(wait)
                page -= 1
                continue

            batch = r.json().get("products", [])
            if not batch:
                break
            products.extend(batch)

            # ── Cursor-based next page (Shopify's recommended method) ──────────
            next_url   = None
            link_header = r.headers.get("Link", "")
            if link_header:
                for part in link_header.split(","):
                    part = part.strip()
                    if 'rel="next"' in part:
                        m = re.search(r'<([^>]+)>', part)
                        if m:
                            next_url = m.group(1)
                            break

            # ── Fallback: page param when Link header absent but batch full ────
            if not next_url and len(batch) == 250:
                next_url = (
                    f"https://{shop_domain}/products.json?limit=250&page={page + 1}"
                )
                logger.debug("[%s] no Link header — page fallback: page %d",
                             shop_domain, page + 1)

            # Polite randomised delay between pages
            if next_url:
                await asyncio.sleep(random.uniform(0.6, 1.4))

    logger.info("[%s] fetch complete — %d products, %d pages",
                shop_domain, len(products), page)
    return products, shop_name


# ── Streaming XML builder ──────────────────────────────────────────────────────

def _build_feed_streaming(
    path:          str,
    shop_name:     str,
    shop_url:      str,
    products:      list[dict],
    currency:      str,
    shop_currency: str,
    rates:         dict[str, float],
) -> tuple[int, int]:
    """
    Write a single-region XML feed directly to *path* using lxml's streaming
    xmlfile API.  No full element tree is built — memory stays flat regardless
    of catalogue size.
    Returns (included_items, skipped_items).
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)

    # Write to a temp file in the same dir, then atomically rename
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            with etree.xmlfile(fh, encoding="UTF-8") as xf:
                xf.write_declaration()
                with xf.element("rss", nsmap={"g": G_NS}, version="2.0"):
                    with xf.element("channel"):
                        # Channel-level metadata
                        title_el = etree.Element("title")
                        title_el.text = f"{shop_name} — Product Feed"
                        xf.write(title_el)

                        link_el = etree.Element("link")
                        link_el.text = shop_url
                        xf.write(link_el)

                        desc_el = etree.Element("description")
                        desc_el.text = (
                            f"Google Shopping / Pinterest Catalog feed — {currency}"
                        )
                        xf.write(desc_el)

                        # Items
                        seen:     set[str] = set()
                        included = skipped = 0

                        for product in products:
                            p_id  = str(product.get("id", ""))
                            title = (product.get("title") or "").strip()
                            if not title:
                                skipped += 1
                                continue

                            description  = _strip_html(
                                product.get("body_html") or title
                            )[:5000]
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
                                img["src"]
                                for img in (product.get("images") or [])
                                if img.get("src")
                            ]

                            for variant in (product.get("variants") or []):
                                v_id = str(variant.get("id", ""))
                                if v_id in seen:
                                    continue
                                seen.add(v_id)

                                available = variant.get("available", False)

                                try:
                                    raw_price = float(variant.get("price") or 0)
                                except (TypeError, ValueError):
                                    raw_price = 0.0
                                if raw_price <= 0:
                                    skipped += 1
                                    continue

                                # Variant-specific image, fall back to product images
                                v_img_id  = variant.get("image_id")
                                v_img_url = None
                                if v_img_id:
                                    for img in (product.get("images") or []):
                                        if img.get("id") == v_img_id:
                                            v_img_url = img.get("src")
                                            break
                                all_images = list(dict.fromkeys(
                                    filter(None, [v_img_url] + product_images)
                                ))
                                if not all_images:
                                    skipped += 1
                                    continue

                                v_title    = (variant.get("title") or "").strip()
                                item_title = (
                                    f"{title} — {v_title}"
                                    if v_title and v_title.lower() != "default title"
                                    else title
                                )[:150]

                                price_str = _convert_price(
                                    raw_price, shop_currency, currency, rates
                                )
                                item_url  = f"{product_url}?variant={v_id}"
                                sku       = (variant.get("sku") or "").strip()

                                # Build item element in memory, then stream-write it
                                item = etree.Element("item")
                                etree.SubElement(item, f"{G}id").text              = v_id
                                etree.SubElement(item, "title").text               = item_title
                                etree.SubElement(item, "description").text         = description
                                etree.SubElement(item, "link").text                = item_url
                                etree.SubElement(item, f"{G}image_link").text      = all_images[0]
                                for extra in all_images[1:6]:
                                    etree.SubElement(
                                        item, f"{G}additional_image_link"
                                    ).text = extra
                                etree.SubElement(
                                    item, f"{G}availability"
                                ).text = "in stock" if available else "out of stock"
                                etree.SubElement(item, f"{G}price").text           = price_str
                                etree.SubElement(item, f"{G}condition").text       = "new"
                                if vendor:
                                    etree.SubElement(item, f"{G}brand").text       = vendor
                                if product_type:
                                    etree.SubElement(
                                        item, f"{G}product_type"
                                    ).text = product_type
                                etree.SubElement(item, f"{G}item_group_id").text   = p_id
                                if sku:
                                    etree.SubElement(item, f"{G}mpn").text         = sku
                                if tags:
                                    etree.SubElement(
                                        item, f"{G}custom_label_0"
                                    ).text = ", ".join(tags[:10])

                                xf.write(item)
                                included += 1

        os.replace(tmp, path)
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise

    return included, skipped


# ── Main pipeline ──────────────────────────────────────────────────────────────

async def generate_feeds_public(
    shop_domain:   str,
    shop_currency: str = "USD",
    progress_cb:   ProgressCb = _noop_cb,
) -> dict:
    """
    Full pipeline: fetch → generate 23 XML feeds → write to disk.
    Designed for 50k–70k+ product catalogues.
    Calls progress_cb with status dicts throughout for live UI updates.
    """
    shop_domain = shop_domain.strip().lower()
    shop_domain = (
        shop_domain.replace("https://", "").replace("http://", "").split("/")[0]
    )
    if ".myshopify.com" in shop_domain:
        shop_domain = shop_domain.split(".myshopify.com")[0] + ".myshopify.com"
    elif not shop_domain.endswith(".myshopify.com"):
        shop_domain += ".myshopify.com"

    slug     = shop_to_slug(shop_domain)
    shop_url = f"https://{shop_domain}"

    # ── Phase 1: Fetch all products ───────────────────────────────────────────
    progress_cb({"phase": "fetching", "page": 0, "products": 0,
                 "message": "Connecting to store…"})

    products, shop_name = await fetch_products_public(shop_domain, progress_cb)

    if not products:
        raise RuntimeError("No products found. Store may be password-protected.")

    total_products = len(products)
    logger.info("[%s] %d products fetched — starting XML generation for %d regions",
                shop_domain, total_products, len(FEED_REGIONS))

    # ── Phase 2: Exchange rates ───────────────────────────────────────────────
    progress_cb({"phase": "rates", "products": total_products,
                 "message": f"{total_products:,} products fetched — loading exchange rates…"})

    rates = await get_exchange_rates(base="USD", ttl=3600)

    # ── Phase 3: Generate XML for each region (streaming) ─────────────────────
    total_regions = len(FEED_REGIONS)
    last_included = 0

    for idx, (region, (country, currency, _flag)) in enumerate(FEED_REGIONS.items(), 1):
        progress_cb({
            "phase":         "generating",
            "region":        region,
            "region_name":   country,
            "region_num":    idx,
            "total_regions": total_regions,
            "products":      total_products,
            "message": (
                f"Building XML feed {idx}/{total_regions}: "
                f"{country} ({currency})…"
            ),
        })

        path = os.path.join(settings.FEEDS_DIR, slug, f"{region}.xml")
        included, skipped = _build_feed_streaming(
            path, shop_name, shop_url,
            products, currency, shop_currency, rates,
        )
        last_included = included
        logger.info("[%s] %s feed written — %d items (%d skipped)",
                    shop_domain, region.upper(), included, skipped)

    feed_urls = {
        region: f"{settings.APP_URL}/feed/{slug}/{region}.xml"
        for region in FEED_REGIONS
    }

    progress_cb({
        "phase":     "done",
        "products":  total_products,
        "items":     last_included,
        "feed_urls": feed_urls,
        "message":   "All feeds generated successfully!",
    })

    logger.info(
        "[%s] generation complete — %d products, %d items/feed, %d regions",
        shop_domain, total_products, last_included, total_regions,
    )

    return {
        "shop_domain": shop_domain,
        "shop_name":   shop_name,
        "slug":        slug,
        "products":    total_products,
        "items":       last_included,
        "feed_count":  total_regions,
        "feed_urls":   feed_urls,
    }

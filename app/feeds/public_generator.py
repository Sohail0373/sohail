"""
Public feed generator — no Shopify app or OAuth required.

Strategy for 50k–70k+ product stores:
  Phase 1  → /products.json cursor pagination  (~25k fast)
  Phase 2  → /collections/{handle}/products.json for every collection,
             adding only product IDs not yet seen — covers the rest
  XML      → lxml.etree.xmlfile streaming (no giant in-memory tree)
  Retries  → exponential back-off on 403/429/430 (bot detection)
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

_HEADERS = {
    "User-Agent": (
        "Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
        "AppleWebKit/537.36 (KHTML, like Gecko) "
        "Chrome/124.0.0.0 Safari/537.36"
    ),
    "Accept": "application/json, text/plain, */*",
    "Accept-Language": "en-US,en;q=0.9",
}

MAX_RETRIES = 6


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


def _parse_link_next(headers) -> str | None:
    """Extract the 'next' URL from a Shopify Link response header."""
    for part in headers.get("Link", "").split(","):
        part = part.strip()
        if 'rel="next"' in part:
            m = re.search(r'<([^>]+)>', part)
            if m:
                return m.group(1)
    return None


async def _resilient_get(
    client: httpx.AsyncClient,
    url: str,
    retries: int = MAX_RETRIES,
    label: str = "",
) -> httpx.Response | None:
    """
    GET with exponential back-off on rate-limit / bot-detection responses.
    Returns None when all retries are exhausted.
    """
    attempt = 0
    while attempt <= retries:
        try:
            r = await client.get(url)
            if r.status_code in (403, 429, 430):
                attempt += 1
                if attempt > retries:
                    logger.error("[%s] %s — giving up after %d attempts",
                                 label, r.status_code, retries)
                    return None
                wait = 15 * (2 ** (attempt - 1)) + random.uniform(0, 8)
                logger.warning("[%s] HTTP %d — sleeping %.0fs (attempt %d/%d)",
                               label, r.status_code, wait, attempt, retries)
                await asyncio.sleep(wait)
                continue
            r.raise_for_status()
            return r
        except (httpx.TimeoutException, httpx.NetworkError) as exc:
            attempt += 1
            if attempt > retries:
                logger.error("[%s] network error — giving up: %s", label, exc)
                return None
            wait = 8 * attempt + random.uniform(0, 4)
            logger.warning("[%s] network error (attempt %d/%d): %s — %.0fs",
                           label, attempt, retries, exc, wait)
            await asyncio.sleep(wait)
        except httpx.HTTPStatusError as exc:
            logger.error("[%s] HTTP %d — %s", label, exc.response.status_code, url)
            return None
    return None


# ── Phase 1: products.json cursor pagination ───────────────────────────────────

async def _phase1_products_json(
    shop_domain: str,
    client: httpx.AsyncClient,
    products_by_id: dict[str, dict],
    progress_cb: ProgressCb,
) -> int:
    """
    Fetch products via /products.json cursor-based pagination.
    Shopify caps this at ~100 pages (~25k products) on the public API.
    Returns number of pages fetched.
    """
    next_url: str | None = f"https://{shop_domain}/products.json?limit=250"
    page = 0

    while next_url:
        page += 1
        total = len(products_by_id)
        progress_cb({
            "phase":    "fetching",
            "page":     page,
            "products": total,
            "message":  f"[Phase 1] Fetching page {page} — {total:,} products so far…",
        })
        logger.info("[%s] Phase1 page %d — %d products", shop_domain, page, total)

        r = await _resilient_get(client, next_url, label=shop_domain)
        if r is None:
            break

        batch = r.json().get("products", [])
        if not batch:
            break

        for p in batch:
            pid = str(p.get("id", ""))
            if pid:
                products_by_id[pid] = p

        # Cursor-based next page
        next_url = _parse_link_next(r.headers)

        # Fallback: page param if cursor absent but batch is full
        if not next_url and len(batch) == 250:
            next_url = (
                f"https://{shop_domain}/products.json?limit=250&page={page + 1}"
            )

        if next_url:
            await asyncio.sleep(random.uniform(0.6, 1.2))

    return page


# ── Phase 2: collection-based supplement ───────────────────────────────────────

async def _get_all_collection_handles(
    shop_domain: str,
    client: httpx.AsyncClient,
) -> list[str]:
    """
    Return every collection handle via /collections.json (public, no auth).
    Uses cursor/page pagination same as products.json.
    """
    handles: list[str] = []
    next_url: str | None = f"https://{shop_domain}/collections.json?limit=250"
    page = 0

    while next_url:
        page += 1
        r = await _resilient_get(client, next_url, label=f"{shop_domain}/collections")
        if r is None:
            break
        batch = r.json().get("collections", [])
        if not batch:
            break
        handles.extend(c["handle"] for c in batch if c.get("handle"))

        next_url = _parse_link_next(r.headers)
        if not next_url and len(batch) == 250:
            next_url = (
                f"https://{shop_domain}/collections.json?limit=250&page={page + 1}"
            )
        if next_url:
            await asyncio.sleep(random.uniform(0.4, 0.9))

    logger.info("[%s] found %d collections", shop_domain, len(handles))
    return handles


async def _fetch_new_products_from_collection(
    shop_domain: str,
    handle: str,
    client: httpx.AsyncClient,
    seen_ids: set[str],
) -> list[dict]:
    """
    Fetch products from one collection, returning only those
    whose IDs are NOT already in seen_ids.
    Updates seen_ids in-place.
    """
    new_products: list[dict] = []
    page = 0  # tracks pages fetched so far — used for page-param fallback
    next_url: str | None = (
        f"https://{shop_domain}/collections/{handle}/products.json?limit=250"
    )

    while next_url:
        r = await _resilient_get(
            client, next_url, retries=4,
            label=f"{shop_domain}/{handle}",
        )
        if r is None:
            break
        batch = r.json().get("products", [])
        if not batch:
            break

        page += 1  # increment AFTER confirming we got products

        for p in batch:
            pid = str(p.get("id", ""))
            if pid and pid not in seen_ids:
                seen_ids.add(pid)
                new_products.append(p)

        # Cursor-based next page (preferred)
        next_url = _parse_link_next(r.headers)

        # Page-param fallback: batch was full but Shopify gave no cursor.
        # `page` is now the page we just fetched, so next = page + 1.
        if not next_url and len(batch) == 250:
            next_url = (
                f"https://{shop_domain}/collections/{handle}/products.json"
                f"?limit=250&page={page + 1}"
            )

        if next_url:
            await asyncio.sleep(random.uniform(0.4, 0.8))

    return new_products


async def _phase2_collections(
    shop_domain: str,
    client: httpx.AsyncClient,
    products_by_id: dict[str, dict],
    progress_cb: ProgressCb,
) -> None:
    """
    Supplement products_by_id with every product missed by /products.json.

    Step A — /collections/all  (built into EVERY Shopify store, even ones
              with zero manual collections — contains ALL published products)
    Step B — individual named collections (catches any remaining gaps)

    Both steps deduplicate against seen_ids so no product is double-counted.
    """
    seen_ids    = set(products_by_id.keys())
    phase1_count = len(products_by_id)

    # ── Step A: /collections/all ──────────────────────────────────────────────
    # Every Shopify store exposes this special pseudo-collection regardless of
    # whether the merchant has created any manual/smart collections.
    progress_cb({
        "phase":    "fetching",
        "page":     -1,
        "products": phase1_count,
        "message":  f"[Phase 2] Fetching /collections/all… ({phase1_count:,} from Phase 1)",
    })
    logger.info("[%s] Phase 2 Step A: /collections/all", shop_domain)

    all_new = await _fetch_new_products_from_collection(
        shop_domain, "all", client, seen_ids
    )
    for p in all_new:
        pid = str(p.get("id", ""))
        if pid:
            products_by_id[pid] = p

    after_all = len(products_by_id)
    logger.info("[%s] /collections/all gave %d new products (total now %d)",
                shop_domain, len(all_new), after_all)

    progress_cb({
        "phase":    "fetching",
        "page":     -1,
        "products": after_all,
        "message": (
            f"[Phase 2] /collections/all done — +{len(all_new):,} new "
            f"(total {after_all:,}). Scanning named collections…"
        ),
    })

    # ── Step B: named collections (mop-up pass) ───────────────────────────────
    # Some products appear in collections but not in /collections/all if their
    # storefront availability is restricted to specific channels.
    handles = await _get_all_collection_handles(shop_domain, client)
    if not handles:
        logger.info("[%s] no named collections — Phase 2 complete with %d products",
                    shop_domain, after_all)
        return

    total_new_b = 0
    for i, handle in enumerate(handles, 1):
        new_prods = await _fetch_new_products_from_collection(
            shop_domain, handle, client, seen_ids
        )
        for p in new_prods:
            pid = str(p.get("id", ""))
            if pid:
                products_by_id[pid] = p
        total_new_b += len(new_prods)

        if new_prods or i % 10 == 0:
            total = len(products_by_id)
            progress_cb({
                "phase":    "fetching",
                "page":     i,
                "products": total,
                "message": (
                    f"[Phase 2] Collection {i}/{len(handles)} — "
                    f"+{len(new_prods)} new — total {total:,} products…"
                ),
            })

        logger.debug("[%s] '%s' → %d new (total %d)",
                     shop_domain, handle, len(new_prods), len(products_by_id))

    logger.info(
        "[%s] Phase 2 done — /collections/all: +%d, named collections: +%d, "
        "grand total: %d",
        shop_domain, len(all_new), total_new_b, len(products_by_id),
    )


# ── Shop name helper ───────────────────────────────────────────────────────────

async def _fetch_shop_name(shop_domain: str, client: httpx.AsyncClient) -> str:
    try:
        r = await client.get(f"https://{shop_domain}/", timeout=12)
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


# ── Main product fetcher ───────────────────────────────────────────────────────

async def fetch_products_public(
    shop_domain: str,
    progress_cb: ProgressCb = _noop_cb,
) -> tuple[list[dict], str]:
    """
    Fetch ALL products from a public Shopify store — no auth required.

    Two-phase strategy:
      Phase 1: /products.json cursor pagination (fast; Shopify caps ~25k)
      Phase 2: /collections/{handle}/products.json for every collection,
               adding only unseen product IDs (covers the remaining 25k–70k+)

    Returns (products_list, shop_name).
    """
    products_by_id: dict[str, dict] = {}

    async with httpx.AsyncClient(
        timeout=httpx.Timeout(60.0, connect=15.0),
        headers=_HEADERS,
        follow_redirects=True,
    ) as client:
        shop_name = await _fetch_shop_name(shop_domain, client)

        progress_cb({
            "phase": "fetching", "page": 0, "products": 0,
            "message": "Starting Phase 1: fetching via products.json…",
        })

        # Phase 1 ─────────────────────────────────────────────────────────────
        await _phase1_products_json(shop_domain, client, products_by_id, progress_cb)

        phase1_count = len(products_by_id)
        logger.info("[%s] Phase 1 complete — %d products", shop_domain, phase1_count)

        # Phase 2 ─────────────────────────────────────────────────────────────
        await _phase2_collections(shop_domain, client, products_by_id, progress_cb)

    products = list(products_by_id.values())
    logger.info("[%s] fetch complete — %d total products", shop_domain, len(products))
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
    Write one region's XML feed directly to disk using lxml streaming.
    No full element tree — memory stays flat for 70k+ products.
    Returns (included_items, skipped_items).
    """
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as fh:
            with etree.xmlfile(fh, encoding="UTF-8") as xf:
                xf.write_declaration()
                with xf.element("rss", nsmap={"g": G_NS}, version="2.0"):
                    with xf.element("channel"):
                        t = etree.Element("title")
                        t.text = f"{shop_name} — Product Feed"
                        xf.write(t)
                        lk = etree.Element("link")
                        lk.text = shop_url
                        xf.write(lk)
                        d = etree.Element("description")
                        d.text = f"Google Shopping / Pinterest Catalog feed — {currency}"
                        xf.write(d)

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
                                item_url = f"{product_url}?variant={v_id}"
                                sku      = (variant.get("sku") or "").strip()

                                item = etree.Element("item")
                                etree.SubElement(item, f"{G}id").text          = v_id
                                etree.SubElement(item, "title").text           = item_title
                                etree.SubElement(item, "description").text     = description
                                etree.SubElement(item, "link").text            = item_url
                                etree.SubElement(item, f"{G}image_link").text  = all_images[0]
                                for extra in all_images[1:6]:
                                    etree.SubElement(
                                        item, f"{G}additional_image_link"
                                    ).text = extra
                                etree.SubElement(item, f"{G}availability").text = (
                                    "in stock" if variant.get("available", False)
                                    else "out of stock"
                                )
                                etree.SubElement(item, f"{G}price").text       = price_str
                                etree.SubElement(item, f"{G}condition").text   = "new"
                                if vendor:
                                    etree.SubElement(item, f"{G}brand").text   = vendor
                                if product_type:
                                    etree.SubElement(
                                        item, f"{G}product_type"
                                    ).text = product_type
                                etree.SubElement(item, f"{G}item_group_id").text = p_id
                                if sku:
                                    etree.SubElement(item, f"{G}mpn").text     = sku
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
    Full pipeline: fetch (2-phase) → exchange rates → 23 × XML files.
    Handles 50k–70k+ product stores via collection-based supplement.
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

    progress_cb({"phase": "fetching", "page": 0, "products": 0,
                 "message": "Connecting to store…"})

    products, shop_name = await fetch_products_public(shop_domain, progress_cb)

    if not products:
        raise RuntimeError("No products found. Store may be password-protected.")

    total_products = len(products)
    logger.info("[%s] %d products total — starting XML for %d regions",
                shop_domain, total_products, len(FEED_REGIONS))

    # Exchange rates
    progress_cb({
        "phase":    "rates",
        "products": total_products,
        "message":  f"{total_products:,} products fetched — loading exchange rates…",
    })
    rates = await get_exchange_rates(base="USD", ttl=3600)

    # XML generation — one region at a time (streaming)
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
            "message":       f"Building XML {idx}/{total_regions}: {country} ({currency})…",
        })

        path = os.path.join(settings.FEEDS_DIR, slug, f"{region}.xml")
        included, skipped = _build_feed_streaming(
            path, shop_name, shop_url,
            products, currency, shop_currency, rates,
        )
        last_included = included
        logger.info("[%s] %s done — %d items (%d skipped)",
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
        "message":   f"Done! {total_products:,} products → {last_included:,} feed items.",
    })

    logger.info("[%s] complete — %d products, %d items/feed, %d regions",
                shop_domain, total_products, last_included, total_regions)

    return {
        "shop_domain": shop_domain,
        "shop_name":   shop_name,
        "slug":        slug,
        "products":    total_products,
        "items":       last_included,
        "feed_count":  total_regions,
        "feed_urls":   feed_urls,
    }

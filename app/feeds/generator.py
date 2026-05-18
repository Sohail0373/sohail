from __future__ import annotations

import logging
import os
import re
import tempfile
from datetime import datetime, timezone

from lxml import etree
from sqlalchemy.orm import Session

from ..config import settings
from ..database import SessionLocal
from ..exchange_rates import FEED_CURRENCIES, convert_price, get_exchange_rates
from ..models import Store
from .shopify_api import ShopifyClient

logger = logging.getLogger(__name__)

G_NS = "http://base.google.com/ns/1.0"
G = f"{{{G_NS}}}"

_HTML_TAG_RE = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")


# ── Helpers ────────────────────────────────────────────────────────────────────

def _strip_html(text: str) -> str:
    stripped = _HTML_TAG_RE.sub(" ", text)
    return _WHITESPACE_RE.sub(" ", stripped).strip()


def _gid_number(gid: str) -> str:
    """Extract numeric ID from a Shopify GID: 'gid://shopify/Product/123' → '123'."""
    return gid.rsplit("/", 1)[-1]


def _atomic_write(path: str, data: bytes) -> None:
    """Write *data* to *path* atomically using a temp file + rename."""
    os.makedirs(os.path.dirname(path), exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=os.path.dirname(path), suffix=".tmp")
    try:
        with os.fdopen(fd, "wb") as f:
            f.write(data)
        os.replace(tmp, path)   # atomic on POSIX
    except Exception:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise


# ── XML feed builder ───────────────────────────────────────────────────────────

def _build_feed(
    shop_domain: str,
    shop_name: str,
    shop_url: str,
    products: list[dict],
    currency: str,
    shop_currency: str,
    rates: dict[str, float],
) -> bytes:
    """Build a Google Shopping / Pinterest-compatible RSS XML feed and return bytes."""
    nsmap = {"g": G_NS}
    rss = etree.Element("rss", nsmap=nsmap)
    rss.set("version", "2.0")

    channel = etree.SubElement(rss, "channel")
    etree.SubElement(channel, "title").text = f"{shop_name} — Product Feed"
    etree.SubElement(channel, "link").text = shop_url
    etree.SubElement(channel, "description").text = (
        f"Google Shopping / Pinterest Catalog feed for {shop_name} — {currency}"
    )

    seen: set[str] = set()
    included = 0
    skipped = 0

    for product in products:
        p_id = _gid_number(product["id"])
        title = (product.get("title") or "").strip()
        if not title:
            logger.debug("Skip product %s: no title", p_id)
            skipped += 1
            continue

        description = _strip_html(product.get("descriptionHtml") or title)[:5000]
        handle = product.get("handle", "")
        product_url = f"{shop_url}/products/{handle}"
        product_type = (product.get("productType") or "").strip()
        vendor = (product.get("vendor") or "").strip()
        tags: list[str] = product.get("tags") or []

        # All product-level images in order
        product_images = [
            edge["node"]["url"]
            for edge in (product.get("images") or {}).get("edges", [])
            if edge.get("node", {}).get("url")
        ]

        for v_edge in (product.get("variants") or {}).get("edges", []):
            v = v_edge["node"]
            v_gid = v["id"]

            if v_gid in seen:
                continue
            seen.add(v_gid)

            v_id = _gid_number(v_gid)

            # Skip unavailable variants
            if not v.get("availableForSale"):
                continue

            inv = v.get("inventoryQuantity")
            if inv is not None and inv <= 0:
                continue

            # Price validation
            try:
                raw_price = float(v.get("price") or 0)
            except (TypeError, ValueError):
                raw_price = 0.0
            if raw_price <= 0:
                logger.debug("Skip variant %s: price=%s", v_id, raw_price)
                skipped += 1
                continue

            # Images: prefer variant-specific image, then fall back to product images
            v_img_url = (v.get("image") or {}).get("url")
            all_images = list(dict.fromkeys(filter(None, [v_img_url] + product_images)))

            if not all_images:
                logger.debug("Skip variant %s: no image", v_id)
                skipped += 1
                continue

            # Build item title (suppress "Default Title" for single-variant products)
            v_title = (v.get("title") or "").strip()
            item_title = (
                f"{title} — {v_title}"
                if v_title and v_title.lower() != "default title"
                else title
            )[:150]

            price_str = convert_price(raw_price, shop_currency, currency, rates)
            item_url = f"{product_url}?variant={v_id}"

            # ── Build <item> ──────────────────────────────────────────────────
            item = etree.SubElement(channel, "item")
            etree.SubElement(item, f"{G}id").text = v_id
            etree.SubElement(item, "title").text = item_title
            etree.SubElement(item, "description").text = description
            etree.SubElement(item, "link").text = item_url
            etree.SubElement(item, f"{G}image_link").text = all_images[0]
            for extra in all_images[1:6]:          # up to 5 additional images
                etree.SubElement(item, f"{G}additional_image_link").text = extra
            etree.SubElement(item, f"{G}availability").text = "in stock"
            etree.SubElement(item, f"{G}price").text = price_str
            etree.SubElement(item, f"{G}condition").text = "new"
            if vendor:
                etree.SubElement(item, f"{G}brand").text = vendor
            if product_type:
                etree.SubElement(item, f"{G}product_type").text = product_type
            etree.SubElement(item, f"{G}item_group_id").text = p_id
            if v.get("sku"):
                etree.SubElement(item, f"{G}mpn").text = v["sku"]
            if tags:
                etree.SubElement(item, f"{G}custom_label_0").text = ", ".join(tags[:10])

            included += 1

    logger.info(
        "[%s] %s feed: %d items included, %d skipped",
        shop_domain, currency, included, skipped,
    )
    return etree.tostring(rss, xml_declaration=True, encoding="UTF-8", pretty_print=True)


# ── Public entry point ─────────────────────────────────────────────────────────

async def generate_feeds_for_shop(shop_domain: str, access_token: str) -> int:
    """Generate all 5 regional XML feeds for *shop_domain*.

    Returns the total number of products fetched.
    Writes feeds atomically to FEEDS_DIR/{slug}/{region}.xml.
    """
    logger.info("Feed generation started for %s", shop_domain)
    client = ShopifyClient(shop_domain, access_token)

    # Fetch shop metadata
    try:
        shop_info = await client.get_shop_info()
    except Exception as exc:
        logger.error("Could not fetch shop info for %s: %s — using defaults", shop_domain, exc)
        shop_info = {}

    shop_name: str = shop_info.get("name") or shop_domain
    shop_currency: str = shop_info.get("currencyCode") or "USD"
    shop_url: str = (shop_info.get("primaryDomain") or {}).get("url") or f"https://{shop_domain}"

    # Collect all active products
    products: list[dict] = []
    try:
        async for product in client.iter_products():
            products.append(product)
    except Exception as exc:
        logger.error("Product fetch failed for %s after %d products: %s", shop_domain, len(products), exc)
        if not products:
            return 0

    logger.info("Fetched %d products for %s", len(products), shop_domain)

    # Get (possibly cached) exchange rates
    rates = await get_exchange_rates(base="USD", ttl=settings.EXCHANGE_RATE_TTL)

    # Build and write one XML file per region
    from ..auth.shopify_oauth import shop_to_slug
    slug = shop_to_slug(shop_domain)

    for region, currency in FEED_CURRENCIES.items():
        xml_bytes = _build_feed(
            shop_domain=shop_domain,
            shop_name=shop_name,
            shop_url=shop_url,
            products=products,
            currency=currency,
            shop_currency=shop_currency,
            rates=rates,
        )
        path = os.path.join(settings.FEEDS_DIR, slug, f"{region}.xml")
        _atomic_write(path, xml_bytes)
        logger.info("Written: %s", path)

    # Persist stats back to DB
    db: Session = SessionLocal()
    try:
        store = db.query(Store).filter(Store.shop_domain == shop_domain).first()
        if store:
            store.last_feed_generated = datetime.now(timezone.utc)
            store.product_count = len(products)
            store.shop_name = shop_name
            store.shop_currency = shop_currency
            store.shop_url = shop_url
            db.commit()
    finally:
        db.close()

    logger.info("Feed generation complete for %s (%d products)", shop_domain, len(products))
    return len(products)

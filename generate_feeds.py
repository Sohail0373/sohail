#!/usr/bin/env python3
"""
Growvoria Feed Generator — No Auth Required
============================================
Koi app, koi token, koi OAuth — kuch nahi chahiye.
Sirf store domain do → saare 23 regions ki feeds ready.

Shopify ka public products.json use karta hai jo bina
kisi authentication ke available hota hai.

Usage:
    python3 generate_feeds.py
"""

from __future__ import annotations

import os
import re
import sys
import time

# Auto-install dependencies if missing
def _ensure(pkg: str, import_name: str | None = None) -> None:
    import importlib
    try:
        importlib.import_module(import_name or pkg)
    except ImportError:
        import subprocess
        print(f"  Installing {pkg}...")
        subprocess.check_call([sys.executable, "-m", "pip", "install", pkg, "-q"])

_ensure("httpx")
_ensure("lxml")

import httpx
from lxml import etree

# ── Region / Currency config ───────────────────────────────────────────────────

FEED_REGIONS: dict[str, tuple[str, str]] = {
    "us": ("United States",  "USD"),
    "ae": ("UAE",            "AED"),
    "uk": ("United Kingdom", "GBP"),
    "sa": ("Saudi Arabia",   "SAR"),
    "jp": ("Japan",          "JPY"),
    "kr": ("South Korea",    "KRW"),
    "au": ("Australia",      "AUD"),
    "sg": ("Singapore",      "SGD"),
    "hk": ("Hong Kong",      "HKD"),
    "qa": ("Qatar",          "QAR"),
    "kw": ("Kuwait",         "KWD"),
    "bh": ("Bahrain",        "BHD"),
    "ca": ("Canada",         "CAD"),
    "nl": ("Netherlands",    "EUR"),
    "br": ("Brazil",         "BRL"),
    "my": ("Malaysia",       "MYR"),
    "th": ("Thailand",       "THB"),
    "no": ("Norway",         "NOK"),
    "se": ("Sweden",         "SEK"),
    "ch": ("Switzerland",    "CHF"),
    "fr": ("France",         "EUR"),
    "it": ("Italy",          "EUR"),
    "de": ("Germany",        "EUR"),
}

FALLBACK_RATES: dict[str, float] = {
    "USD": 1.000, "AED": 3.673, "GBP": 0.751, "SAR": 3.750,
    "JPY": 158.76, "KRW": 1497.53, "AUD": 1.400, "SGD": 1.280,
    "HKD": 7.830, "QAR": 3.640, "KWD": 0.308, "BHD": 0.376,
    "CAD": 1.380, "EUR": 0.861, "BRL": 5.050, "MYR": 3.950,
    "THB": 32.68, "NOK": 9.320, "SEK": 9.440, "CHF": 0.787,
}

G_NS = "http://base.google.com/ns/1.0"
G    = f"{{{G_NS}}}"
_HTML_TAG_RE  = re.compile(r"<[^>]+>")
_WHITESPACE_RE = re.compile(r"\s+")

# ── Helpers ────────────────────────────────────────────────────────────────────

def strip_html(text: str) -> str:
    return _WHITESPACE_RE.sub(" ", _HTML_TAG_RE.sub(" ", text)).strip()

def shop_to_slug(domain: str) -> str:
    return domain.removesuffix(".myshopify.com").replace(".", "-")

def convert_price(amount: float, from_curr: str, to_curr: str, rates: dict) -> str:
    if from_curr == to_curr:
        return f"{amount:.2f} {to_curr}"
    usd       = amount / rates.get(from_curr, 1.0)
    converted = usd * rates.get(to_curr, FALLBACK_RATES.get(to_curr, 1.0))
    return f"{converted:.2f} {to_curr}"

# ── Public Shopify product fetcher (no auth) ───────────────────────────────────

def fetch_all_products(shop_domain: str) -> tuple[list[dict], str, str, str]:
    """
    Fetch all products from Shopify's public products.json endpoint.
    Uses cursor-based pagination (Link header) for large stores.
    Returns (products, shop_name, shop_currency, shop_url).
    No API token required.
    """
    base_url  = f"https://{shop_domain}"
    shop_name = shop_domain
    shop_url  = base_url

    products: list[dict] = []
    headers = {"User-Agent": "Mozilla/5.0 (FeedGenerator/1.0)"}

    with httpx.Client(timeout=30, headers=headers, follow_redirects=True) as client:
        # Try to get shop name from meta
        try:
            r = client.get(f"{base_url}/", timeout=10)
            m = re.search(r'<meta[^>]+property=["\']og:site_name["\'][^>]+content=["\']([^"\']+)["\']', r.text, re.I)
            if m:
                shop_name = m.group(1).strip()
            else:
                m2 = re.search(r'<title>([^<]+)</title>', r.text, re.I)
                if m2:
                    shop_name = m2.group(1).strip().split("|")[0].strip()
        except Exception:
            pass

        # Start with first page
        next_url: str | None = f"{base_url}/products.json?limit=250"
        page = 1

        while next_url:
            print(f"  Fetching page {page} ({len(products)} so far)...", end="\r", flush=True)
            try:
                r = client.get(next_url)
                r.raise_for_status()
            except httpx.HTTPStatusError as exc:
                if exc.response.status_code in (429, 430):
                    print(f"\n  Rate limited — waiting 10 seconds...")
                    time.sleep(10)
                    continue
                raise

            batch = r.json().get("products", [])
            if not batch:
                break
            products.extend(batch)

            # Parse Link header for cursor-based next page
            next_url = None
            link_header = r.headers.get("Link", "")
            if link_header:
                # Format: <url>; rel="next", <url>; rel="previous"
                for part in link_header.split(","):
                    part = part.strip()
                    if 'rel="next"' in part:
                        m = re.search(r'<([^>]+)>', part)
                        if m:
                            next_url = m.group(1)
                            break

            if len(batch) < 250 and not next_url:
                break

            page += 1
            time.sleep(0.3)

    print(f"  ✓ {len(products)} products fetched                          ")
    shop_currency = "USD"
    return products, shop_name, shop_currency, shop_url


def get_exchange_rates() -> dict[str, float]:
    try:
        with httpx.Client(timeout=10) as client:
            r = client.get("https://api.exchangerate-api.com/v4/latest/USD")
            r.raise_for_status()
            rates: dict = {"USD": 1.0, **r.json()["rates"]}
            for code, val in FALLBACK_RATES.items():
                rates.setdefault(code, val)
            print("  ✓ Live exchange rates fetched")
            return rates
    except Exception as exc:
        print(f"  ⚠  Live rates unavailable ({exc}) — using fallback rates")
        return FALLBACK_RATES.copy()

# ── XML feed builder ───────────────────────────────────────────────────────────

def build_feed(
    shop_domain: str,
    shop_name:   str,
    shop_url:    str,
    products:    list[dict],
    currency:    str,
    shop_currency: str,
    rates:       dict[str, float],
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
        p_id        = str(product.get("id", ""))
        title       = (product.get("title") or "").strip()
        if not title:
            skipped += 1
            continue

        body_html   = product.get("body_html") or ""
        description = strip_html(body_html or title)[:5000]
        handle      = product.get("handle", "")
        product_url = f"{shop_url}/products/{handle}"
        product_type = (product.get("product_type") or "").strip()
        vendor      = (product.get("vendor") or "").strip()
        tags_raw    = product.get("tags") or ""
        tags: list[str] = (
            [t.strip() for t in tags_raw.split(",") if t.strip()]
            if isinstance(tags_raw, str)
            else list(tags_raw)
        )

        # Product-level images
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
            availability_str = "in stock" if available else "out of stock"

            try:
                raw_price = float(variant.get("price") or 0)
            except (TypeError, ValueError):
                raw_price = 0.0
            if raw_price <= 0:
                skipped += 1
                continue

            # Variant image → fall back to product images
            v_img_id = variant.get("image_id")
            v_img_url: str | None = None
            if v_img_id:
                for img in (product.get("images") or []):
                    if img.get("id") == v_img_id:
                        v_img_url = img.get("src")
                        break
            all_images = list(dict.fromkeys(filter(None, [v_img_url] + product_images)))

            if not all_images:
                skipped += 1
                continue

            v_title = (variant.get("title") or "").strip()
            item_title = (
                f"{title} — {v_title}"
                if v_title and v_title.lower() != "default title"
                else title
            )[:150]

            price_str = convert_price(raw_price, shop_currency, currency, rates)
            item_url  = f"{product_url}?variant={v_id}"

            item = etree.SubElement(channel, "item")
            etree.SubElement(item, f"{G}id").text              = v_id
            etree.SubElement(item, "title").text               = item_title
            etree.SubElement(item, "description").text         = description
            etree.SubElement(item, "link").text                = item_url
            etree.SubElement(item, f"{G}image_link").text      = all_images[0]
            for extra in all_images[1:6]:
                etree.SubElement(item, f"{G}additional_image_link").text = extra
            etree.SubElement(item, f"{G}availability").text    = availability_str
            etree.SubElement(item, f"{G}price").text           = price_str
            etree.SubElement(item, f"{G}condition").text       = "new"
            if vendor:
                etree.SubElement(item, f"{G}brand").text       = vendor
            if product_type:
                etree.SubElement(item, f"{G}product_type").text = product_type
            etree.SubElement(item, f"{G}item_group_id").text   = p_id
            sku = (variant.get("sku") or "").strip()
            if sku:
                etree.SubElement(item, f"{G}mpn").text         = sku
            if tags:
                etree.SubElement(item, f"{G}custom_label_0").text = ", ".join(tags[:10])

            included += 1

    xml_bytes = etree.tostring(
        rss, xml_declaration=True, encoding="UTF-8", pretty_print=True
    )
    return xml_bytes, included, skipped

# ── Main ───────────────────────────────────────────────────────────────────────

VPS_FEEDS_DIR = "/opt/growvoria/feeds"

def main() -> None:
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║       Growvoria Feed Generator — No Auth         ║")
    print("║   Koi token nahi • Koi app nahi • Koi DB nahi   ║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    # ── Input ─────────────────────────────────────────────────────────────────
    raw = input("Store domain (e.g. elrabags.myshopify.com): ").strip().lower()
    if not raw:
        print("Domain required.")
        sys.exit(1)
    # Strip https://, http://, trailing slashes, paths
    raw = raw.replace("https://", "").replace("http://", "").split("/")[0].strip()
    # Extract just the myshopify subdomain if full URL given
    if ".myshopify.com" in raw:
        raw = raw.split(".myshopify.com")[0] + ".myshopify.com"
    else:
        raw += ".myshopify.com"
    shop_domain = raw

    # Store currency (optional override)
    currency_input = input("Store currency [press Enter for USD]: ").strip().upper()
    shop_currency  = currency_input if currency_input else "USD"

    slug = shop_to_slug(shop_domain)

    # Output directory
    if os.path.isdir(VPS_FEEDS_DIR):
        default_output = os.path.join(VPS_FEEDS_DIR, slug)
        vps_mode = True
    else:
        default_output = os.path.join("feeds", slug)
        vps_mode = False

    custom = input(f"Output folder [Enter = {default_output}]: ").strip()
    output_dir = custom if custom else default_output
    os.makedirs(output_dir, exist_ok=True)

    print()
    print(f"  Store    : {shop_domain}")
    print(f"  Currency : {shop_currency}")
    print(f"  Output   : {os.path.abspath(output_dir)}")
    print()

    # ── Fetch products ────────────────────────────────────────────────────────
    print("Step 1/3  Fetching products (public API — no auth)...")
    try:
        products, shop_name, _, shop_url = fetch_all_products(shop_domain)
    except Exception as exc:
        print(f"\n  ✗ Failed: {exc}")
        print("  Make sure the store is live and not password-protected.")
        sys.exit(1)

    if not products:
        print("  ✗ No products found. Store might be password-protected.")
        sys.exit(1)

    # ── Exchange rates ────────────────────────────────────────────────────────
    print("\nStep 2/3  Fetching exchange rates...")
    rates = get_exchange_rates()

    # ── Generate feeds ────────────────────────────────────────────────────────
    print(f"\nStep 3/3  Generating {len(FEED_REGIONS)} XML feeds...")
    print()

    last_included = 0
    for region, (country, currency) in FEED_REGIONS.items():
        xml_bytes, included, skipped = build_feed(
            shop_domain, shop_name, shop_url,
            products, currency, shop_currency, rates,
        )
        path = os.path.join(output_dir, f"{region}.xml")
        with open(path, "wb") as fh:
            fh.write(xml_bytes)
        last_included = included
        print(f"  ✓ {region.upper():<3}  {country:<16} {currency}  →  {included} items")

    # ── Summary ───────────────────────────────────────────────────────────────
    print()
    print("╔══════════════════════════════════════════════════╗")
    print("║                    COMPLETE ✓                    ║")
    print("╠══════════════════════════════════════════════════╣")
    print(f"║  Store     : {shop_name[:36]:<36}║")
    print(f"║  Products  : {len(products):<36}║")
    print(f"║  Items/feed: {last_included:<36}║")
    print(f"║  Feeds     : {len(FEED_REGIONS):<36}║")
    print("╚══════════════════════════════════════════════════╝")
    print()

    if vps_mode:
        print("Feed URLs (live now):")
        for region in FEED_REGIONS:
            print(f"  https://growvoria.duckdns.org/feed/{slug}/{region}.xml")
    else:
        abs_out = os.path.abspath(output_dir)
        print(f"Files saved in: {abs_out}")
        print()
        print("VPS pe upload karne ke liye:")
        print(f"  scp -i ~/Downloads/ssh-key-2026-05-20.key -r \"{abs_out}\" ubuntu@150.136.8.222:/opt/growvoria/feeds/")

    print()


if __name__ == "__main__":
    main()

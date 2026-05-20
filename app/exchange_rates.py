from __future__ import annotations

import asyncio
import logging
import time

import httpx

logger = logging.getLogger(__name__)

# ── Region slug → (Country name, ISO 4217 currency code, flag emoji) ──────────
FEED_REGIONS: dict[str, tuple[str, str, str]] = {
    "us": ("United States",  "USD", "🇺🇸"),
    "ae": ("UAE",            "AED", "🇦🇪"),
    "uk": ("United Kingdom", "GBP", "🇬🇧"),
    "sa": ("Saudi Arabia",   "SAR", "🇸🇦"),
    "jp": ("Japan",          "JPY", "🇯🇵"),
    "kr": ("South Korea",    "KRW", "🇰🇷"),
    "au": ("Australia",      "AUD", "🇦🇺"),
    "sg": ("Singapore",      "SGD", "🇸🇬"),
    "hk": ("Hong Kong",      "HKD", "🇭🇰"),
    "qa": ("Qatar",          "QAR", "🇶🇦"),
    "kw": ("Kuwait",         "KWD", "🇰🇼"),
    "bh": ("Bahrain",        "BHD", "🇧🇭"),
    "ca": ("Canada",         "CAD", "🇨🇦"),
    "nl": ("Netherlands",    "EUR", "🇳🇱"),
    "br": ("Brazil",         "BRL", "🇧🇷"),
    "my": ("Malaysia",       "MYR", "🇲🇾"),
    "th": ("Thailand",       "THB", "🇹🇭"),
    "no": ("Norway",         "NOK", "🇳🇴"),
    "se": ("Sweden",         "SEK", "🇸🇪"),
    "ch": ("Switzerland",    "CHF", "🇨🇭"),
    "fr": ("France",         "EUR", "🇫🇷"),
    "it": ("Italy",          "EUR", "🇮🇹"),
    "de": ("Germany",        "EUR", "🇩🇪"),
}

# Convenience alias: region slug → currency code (used by generator)
FEED_CURRENCIES: dict[str, str] = {slug: info[1] for slug, info in FEED_REGIONS.items()}

# ── Static fallback rates (USD base, 2025-05) ─────────────────────────────────
# Gulf currencies are fixed pegs; others are approximate market rates.
# Used ONLY when both live APIs are unreachable.
FALLBACK_RATES: dict[str, float] = {
    "USD": 1.000,
    "AED": 3.673,   # fixed peg
    "GBP": 0.751,
    "SAR": 3.750,   # fixed peg
    "JPY": 158.76,
    "KRW": 1497.53,
    "AUD": 1.400,
    "SGD": 1.280,
    "HKD": 7.830,
    "QAR": 3.640,   # fixed peg
    "KWD": 0.308,
    "BHD": 0.376,   # fixed peg
    "CAD": 1.380,
    "EUR": 0.861,
    "BRL": 5.050,
    "MYR": 3.950,
    "THB": 32.68,
    "NOK": 9.320,
    "SEK": 9.440,
    "CHF": 0.787,
}

# ── Live rate fetching with TTL cache ─────────────────────────────────────────
_rates_cache: dict[str, float] = {}
_cache_ts: float = 0.0
_lock = asyncio.Lock()

# Primary: exchangerate-api.com free tier — covers ALL currencies incl. Gulf pegs
_PRIMARY_URL = "https://api.exchangerate-api.com/v4/latest/USD"
# Secondary: frankfurter.app — major currencies only (no Gulf), used as fallback
_SECONDARY_URL = (
    "https://api.frankfurter.app/latest?from=USD"
    "&to=GBP,JPY,AUD,SGD,HKD,CAD,EUR,BRL,MYR,THB,NOK,SEK,CHF"
)


async def get_exchange_rates(base: str = "USD", ttl: int = 3600) -> dict[str, float]:
    """Return live USD-based exchange rates with TTL caching.

    Chain: exchangerate-api.com → frankfurter.app → FALLBACK_RATES.
    Gulf-pegged currencies (AED/SAR/QAR/BHD) always come from the primary API
    or the hardcoded peg values — never guessed from a secondary source.
    """
    global _rates_cache, _cache_ts

    async with _lock:
        if _rates_cache and (time.monotonic() - _cache_ts) < ttl:
            return _rates_cache

        rates = await _fetch_primary()
        if not rates:
            rates = await _fetch_secondary()
        if not rates:
            logger.error("All exchange rate APIs failed — using hardcoded fallback rates")
            rates = FALLBACK_RATES.copy()

        _rates_cache = rates
        _cache_ts = time.monotonic()
        return rates


async def _fetch_primary() -> dict[str, float] | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(_PRIMARY_URL)
            r.raise_for_status()
        data = r.json()
        rates: dict[str, float] = {"USD": 1.0, **data["rates"]}
        # Fill any unexpected gaps from fallback (shouldn't happen with this API)
        for code, fallback_val in FALLBACK_RATES.items():
            rates.setdefault(code, fallback_val)
        logger.info("Exchange rates fetched from exchangerate-api.com (primary)")
        return rates
    except Exception as exc:
        logger.warning("exchangerate-api.com failed: %s", exc)
        return None


async def _fetch_secondary() -> dict[str, float] | None:
    try:
        async with httpx.AsyncClient(timeout=10.0) as client:
            r = await client.get(_SECONDARY_URL)
            r.raise_for_status()
        data = r.json()
        # Start from fallback so Gulf pegged currencies are always present
        rates = FALLBACK_RATES.copy()
        rates.update(data.get("rates", {}))
        logger.info("Exchange rates fetched from frankfurter.app (secondary; Gulf from fallback pegs)")
        return rates
    except Exception as exc:
        logger.warning("frankfurter.app also failed: %s", exc)
        return None


def convert_price(amount: float, from_currency: str, to_currency: str, rates: dict[str, float]) -> str:
    """Convert *amount* in *from_currency* to *to_currency* and format for Google Shopping.

    All rates are USD-indexed, so conversion is:
      amount → USD → target_currency

    Example: $200 USD → AED
      usd_amount = 200 / 1.0 = 200
      converted  = 200 * 3.673 = 734.60
      result     = "734.60 AED"    ← NOT "200 AED"
    """
    if from_currency == to_currency:
        return f"{amount:.2f} {to_currency}"

    # Normalise to USD first
    usd_amount = amount / rates.get(from_currency, 1.0)
    # Convert USD → target
    converted = usd_amount * rates.get(to_currency, FALLBACK_RATES.get(to_currency, 1.0))
    return f"{converted:.2f} {to_currency}"

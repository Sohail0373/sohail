from __future__ import annotations

import logging
from typing import AsyncGenerator

import httpx
from tenacity import (
    before_sleep_log,
    retry,
    retry_if_exception_type,
    stop_after_attempt,
    wait_exponential,
)

from ..config import settings

logger = logging.getLogger(__name__)

# ── GraphQL queries ────────────────────────────────────────────────────────────

SHOP_INFO_QUERY = """
{
  shop {
    name
    currencyCode
    primaryDomain { url }
  }
}
"""

PRODUCTS_QUERY = """
query GetProducts($cursor: String) {
  products(first: 250, after: $cursor, query: "status:active") {
    pageInfo {
      hasNextPage
      endCursor
    }
    edges {
      node {
        id
        title
        descriptionHtml
        handle
        productType
        vendor
        tags
        images(first: 10) {
          edges {
            node { url }
          }
        }
        variants(first: 100) {
          edges {
            node {
              id
              title
              price
              sku
              availableForSale
              inventoryQuantity
              image { url }
              selectedOptions { name value }
            }
          }
        }
      }
    }
  }
}
"""


class ShopifyClient:
    """Async Shopify GraphQL client with automatic pagination and retry logic."""

    def __init__(self, shop: str, token: str) -> None:
        self.shop = shop
        self._url = f"https://{shop}/admin/api/{settings.SHOPIFY_API_VERSION}/graphql.json"
        self._headers = {
            "X-Shopify-Access-Token": token,
            "Content-Type": "application/json",
        }

    @retry(
        retry=retry_if_exception_type((httpx.HTTPStatusError, httpx.TimeoutException, httpx.NetworkError)),
        wait=wait_exponential(multiplier=1, min=2, max=60),
        stop=stop_after_attempt(5),
        before_sleep=before_sleep_log(logger, logging.WARNING),
    )
    async def _query(self, query: str, variables: dict | None = None) -> dict:
        async with httpx.AsyncClient(timeout=60.0) as client:
            resp = await client.post(
                self._url,
                json={"query": query, "variables": variables or {}},
                headers=self._headers,
            )
            resp.raise_for_status()
        data = resp.json()
        if errors := data.get("errors"):
            raise ValueError(f"GraphQL errors for {self.shop}: {errors}")
        return data

    async def get_shop_info(self) -> dict:
        data = await self._query(SHOP_INFO_QUERY)
        return data["data"]["shop"]

    async def iter_products(self) -> AsyncGenerator[dict, None]:
        """Yield every active product node, paginating automatically (250/page)."""
        cursor: str | None = None
        page = 0
        while True:
            page += 1
            variables = {"cursor": cursor} if cursor else {}
            data = await self._query(PRODUCTS_QUERY, variables)
            products_conn = data["data"]["products"]

            for edge in products_conn["edges"]:
                yield edge["node"]

            page_info = products_conn["pageInfo"]
            if not page_info["hasNextPage"]:
                break

            cursor = page_info["endCursor"]
            logger.debug("Fetched page %d for %s (cursor=%s)", page, self.shop, cursor)

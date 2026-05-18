from __future__ import annotations

import os

from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_file=".env",
        env_file_encoding="utf-8",
        extra="ignore",
    )

    # App
    APP_NAME: str = "Growvoria Feeds"
    APP_URL: str = "https://app.growvoria.com"
    DEBUG: bool = False
    LOG_LEVEL: str = "INFO"

    # Shopify
    SHOPIFY_API_KEY: str = ""
    SHOPIFY_API_SECRET: str = ""
    SHOPIFY_SCOPES: str = "read_products,read_inventory"
    SHOPIFY_API_VERSION: str = "2024-10"

    # Database
    DATABASE_URL: str = "sqlite:///./growvoria_feeds.db"

    # Feed storage path
    FEEDS_DIR: str = "./feeds"

    # Security — generate with: python -c "import secrets; print(secrets.token_hex(32))"
    SECRET_KEY: str = "change-me-generate-with-openssl-rand-hex-32"

    # Scheduler
    FEED_REFRESH_HOURS: int = 4

    # Exchange rate API cache TTL (seconds)
    EXCHANGE_RATE_TTL: int = 3600


settings = Settings()

# Ensure the feeds directory exists at startup
os.makedirs(settings.FEEDS_DIR, exist_ok=True)

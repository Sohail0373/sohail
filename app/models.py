from __future__ import annotations

from sqlalchemy import Boolean, Column, DateTime, Integer, String, func

from .database import Base


class Store(Base):
    __tablename__ = "stores"

    id = Column(Integer, primary_key=True, index=True)
    shop_domain = Column(String(255), unique=True, index=True, nullable=False)
    access_token = Column(String(255), nullable=False)
    shop_name = Column(String(255), nullable=True)
    shop_currency = Column(String(10), default="USD")
    shop_url = Column(String(512), nullable=True)          # primary storefront domain
    installed_at = Column(DateTime(timezone=True), server_default=func.now())
    last_feed_generated = Column(DateTime(timezone=True), nullable=True)
    product_count = Column(Integer, default=0)
    is_active = Column(Boolean, default=True)

    def __repr__(self) -> str:
        return f"<Store {self.shop_domain}>"

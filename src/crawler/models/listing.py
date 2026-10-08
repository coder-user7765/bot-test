from __future__ import annotations

from datetime import datetime, timezone
from typing import Any

from pydantic import BaseModel, Field

from crawler.models.image import Image


def utc_now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Price(BaseModel):
    amount: int | float | None = None   # in DZD
    currency: str | None = None
    raw: str | None = None              # exactly what the site shows / sent; never altered
    unit: str | None = None             # e.g. UNIT, MILLION, UNIT_PER_SQUARE (site-provided)


class Location(BaseModel):
    wilaya: str | None = None
    commune: str | None = None
    raw: str | None = None


class Seller(BaseModel):
    display_name: str | None = None
    type: str | None = None             # "store" | "individual" | None


class Listing(BaseModel):
    source: str = "ouedkniss"
    external_id: str
    url: str
    title: str | None = None
    category: str | None = None
    subcategory: str | None = None
    price: Price = Field(default_factory=Price)
    location: Location = Field(default_factory=Location)
    description: str | None = None
    published_at: str | None = None
    seller: Seller = Field(default_factory=Seller)
    attributes: dict[str, Any] = Field(default_factory=dict)
    images: list[Image] = Field(default_factory=list)
    source_metadata: dict[str, Any] = Field(default_factory=dict)
    scraped_at: str = Field(default_factory=utc_now_iso)
    content_hash: str | None = None

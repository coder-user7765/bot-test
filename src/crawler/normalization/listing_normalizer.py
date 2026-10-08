"""Cleans a parsed Listing: text hygiene, price parsing, image de-duplication, content hash.
Nothing is ever invented: unknown stays None and raw values are preserved."""

from __future__ import annotations

import re
import unicodedata
from datetime import datetime, timezone
from typing import Any

from crawler.models.image import Image
from crawler.models.listing import Listing, Price
from crawler.utils.hashing import content_hash

_ZERO_WIDTH = dict.fromkeys(map(ord, "​‌‍⁠﻿‎‏"), None)
_CTRL_RE = re.compile(r"[\x00-\x08\x0b\x0c\x0e-\x1f\x7f]")

# Ouedkniss price units: "million" means a million *centimes* (= 10,000 DA); "billion" = 10^7 DA.
UNIT_MULTIPLIERS_DA = {"million": 10_000, "millions": 10_000, "milliard": 10_000_000,
                       "milliards": 10_000_000, "billion": 10_000_000, "billions": 10_000_000}
_CURRENCY_MAP = {"da": "DZD", "dzd": "DZD", "dinar": "DZD", "dinars": "DZD", "دج": "DZD",
                 "د.ج": "DZD", "دينار": "DZD"}
_PRICE_RE = re.compile(
    r"(?P<num>\d[\d\s  .,]*)\s*(?P<unit>millions?|milliards?|billions?)?\s*"
    r"(?P<cur>da|dzd|dinars?|د\.?ج|دينار)?", re.I)


def clean_text(value: Any, *, keep_newlines: bool = False) -> str | None:
    if value is None:
        return None
    text = unicodedata.normalize("NFC", str(value)).translate(_ZERO_WIDTH)
    text = _CTRL_RE.sub("", text).replace(" ", " ").replace("\r\n", "\n").replace("\r", "\n")
    if keep_newlines:
        text = "\n".join(re.sub(r"[ \t]+", " ", ln).strip() for ln in text.split("\n"))
        text = re.sub(r"\n{3,}", "\n\n", text)
    else:
        text = re.sub(r"\s+", " ", text)
    text = text.strip()
    return text or None


def _parse_number(num: str) -> int | float | None:
    s = re.sub(r"[\s  ]", "", num).strip(".,")
    if not s or not re.search(r"\d", s):
        return None
    if "," in s and "." in s:  # last separator is the decimal one
        dec = "," if s.rfind(",") > s.rfind(".") else "."
        thou = "." if dec == "," else ","
        s = s.replace(thou, "").replace(dec, ".")
    elif "," in s or "." in s:
        sep = "," if "," in s else "."
        parts = s.split(sep)
        if len(parts) > 2 or (len(parts) == 2 and len(parts[1]) == 3 and parts[0] != "0"):
            s = "".join(parts)  # thousands separator(s)
        else:
            s = ".".join(parts)  # decimal separator
    try:
        value = float(s)
    except ValueError:
        return None
    return int(value) if value.is_integer() else value


def parse_price_text(raw: str | None) -> Price:
    """'450 000 DA', '450000 DZD', '450,000 DA', '1,5 Millions' ... -> Price.
    `raw` is always kept verbatim; amount/currency are None when they cannot be read."""
    if raw is None or not str(raw).strip():
        return Price()
    text = str(raw)
    m = _PRICE_RE.search(text.translate(_ZERO_WIDTH))
    if not m:
        return Price(raw=text)
    amount = _parse_number(m.group("num"))
    unit = (m.group("unit") or "").lower() or None
    if amount is not None and unit:
        amount = amount * UNIT_MULTIPLIERS_DA[unit]
        amount = int(amount) if float(amount).is_integer() else amount
    cur_raw = (m.group("cur") or "").lower().replace(" ", "")
    currency = _CURRENCY_MAP.get(cur_raw) if amount is not None else None
    if amount is None:
        return Price(raw=text)
    return Price(amount=amount, currency=currency, raw=text, unit=unit.upper() if unit else None)


def normalize_datetime(value: Any) -> str | None:
    """ISO-8601 in UTC with a trailing Z, or None if unparseable."""
    if not value:
        return None
    s = str(value).strip().replace("Z", "+00:00")
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _clean_value(value: Any) -> Any:
    if isinstance(value, str):
        return clean_text(value)
    if isinstance(value, list):
        cleaned = [_clean_value(v) for v in value]
        cleaned = [v for v in cleaned if v is not None]
        return cleaned[0] if len(cleaned) == 1 else (cleaned or None)
    return value


def _hash_payload(listing: Listing) -> dict[str, Any]:
    return {
        "title": listing.title, "description": listing.description,
        "category": listing.category, "subcategory": listing.subcategory,
        "price": [listing.price.amount, listing.price.unit],
        "location": [listing.location.wilaya, listing.location.commune],
        "seller": listing.seller.display_name, "attributes": listing.attributes,
    }


def normalize_listing(listing: Listing) -> Listing:
    listing.title = clean_text(listing.title)
    listing.category = clean_text(listing.category)
    listing.subcategory = clean_text(listing.subcategory)
    listing.description = clean_text(listing.description, keep_newlines=True)
    listing.published_at = normalize_datetime(listing.published_at)

    price = listing.price
    price.raw = clean_text(price.raw)
    price.currency = clean_text(price.currency)
    if price.amount is not None and price.amount <= 0:
        price.amount = None  # 0 / negative means "no price given" on the site
    if price.amount is None and price.raw:
        parsed = parse_price_text(price.raw)
        price.amount, price.currency = parsed.amount, parsed.currency or price.currency
        price.unit = price.unit or parsed.unit
        if price.amount is not None and price.amount <= 0:
            price.amount = None
    if price.amount is None:
        price.currency = None  # a currency/unit without an amount carries no information
        price.unit = None

    loc = listing.location
    loc.wilaya, loc.commune, loc.raw = clean_text(loc.wilaya), clean_text(loc.commune), clean_text(loc.raw)
    if not loc.raw and (loc.wilaya or loc.commune):
        loc.raw = ", ".join(p for p in (loc.commune, loc.wilaya) if p)

    listing.seller.display_name = clean_text(listing.seller.display_name)

    attrs: dict[str, Any] = {}
    for key, value in listing.attributes.items():
        k, v = clean_text(key), _clean_value(value)
        if k and v is not None:
            attrs[k] = v
    listing.attributes = attrs

    seen: set[str] = set()
    images: list[Image] = []
    for img in listing.images:
        if img.url in seen:
            continue
        seen.add(img.url)
        images.append(img.model_copy(update={"position": len(images), "alt": clean_text(img.alt)}))
    listing.images = images

    has_content = bool(listing.title or listing.description)
    listing.content_hash = content_hash(_hash_payload(listing)) if has_content else None
    return listing

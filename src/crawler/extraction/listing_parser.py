"""Listing parsing.

Primary strategy (what the live site actually provides): `parse_api_announcement`, which maps a
GraphQL `Announcement` object (search summary or detail) onto the Listing model.

Secondary strategies for HTML pages, kept isolated so selectors are easy to update:
    1. embedded JSON      (`window.__X__ = {...}` / <script type="application/json">)
    2. JSON-LD            (schema.org Product / Offer ...)
    3. semantic HTML      (<h1>, <meta og:*>, <time>, itemprop)
    4. CSS selectors      (SELECTORS below; generic, NOT verified against the live site because
                           the live HTML is an empty JavaScript shell)
"""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from crawler.extraction.image_parser import parse_api_media, parse_html_images
from crawler.extraction.structured_data import (extract_embedded_json, extract_json_ld,
                                                find_announcement, find_type)
from crawler.models.listing import Listing, Location, Price, Seller, utc_now_iso
from crawler.utils.urls import extract_external_id, listing_url, normalize_url

# ---- CSS selector strategy (edit here if the site ever serves HTML listings) ------------
SELECTORS: dict[str, list[str]] = {
    "title": ["h1"],
    "price": ["[itemprop=price]", ".price", "[class*=price]"],
    "description": ["[itemprop=description]", "[class*=description]"],
    "location": ["[itemprop=addressLocality]", "[class*=location]", "[class*=city]"],
    "seller": ["[itemprop=seller] [itemprop=name]", "[class*=seller-name]", "[class*=store-name]"],
    "published_at": ["time[datetime]"],
}


def _compact(d: dict[str, Any]) -> dict[str, Any]:
    return {k: v for k, v in d.items() if v not in (None, [], {}, "")}


def _price_raw(node: dict[str, Any]) -> str | None:
    preview, unit = node.get("pricePreview"), node.get("priceUnit")
    if node.get("price") in (None, 0) and preview in (None, 0):
        return None
    shown = preview if preview is not None else node.get("price")
    if isinstance(shown, float) and shown.is_integer():
        shown = int(shown)
    return f"{shown} {unit}".strip() if unit else str(shown)


def _location(node: dict[str, Any]) -> tuple[Location, list[dict[str, Any]]]:
    cities = [c for c in (node.get("cities") or []) if isinstance(c, dict)]
    summary = [_compact({"id": c.get("id"), "commune": c.get("name"),
                         "wilaya": (c.get("region") or {}).get("name")}) for c in cities]
    if not cities:
        return Location(), summary
    first = cities[0]
    commune = first.get("name")
    wilaya = (first.get("region") or {}).get("name")
    parts = [p for p in (node.get("street_name"), commune, wilaya) if p]
    return Location(wilaya=wilaya, commune=commune, raw=", ".join(parts) or None), summary


def _categories(node: dict[str, Any]) -> tuple[str | None, str | None, list[str]]:
    cat = node.get("category") or {}
    tree = [t for t in (cat.get("parentTree") or []) if isinstance(t, dict)]
    path = [t.get("name") for t in tree if t.get("name")]
    leaf = cat.get("name")
    if leaf:
        path.append(leaf)
    if tree:
        return path[0], leaf, path
    return leaf, None, path


def parse_api_announcement(node: dict[str, Any], base_url: str,
                           scraped_at: str | None = None) -> Listing:
    external_id = str(node["id"])
    slug = node.get("slug")
    url = listing_url(base_url, slug, external_id) if slug else normalize_url(
        f"{base_url.rstrip('/')}/annonce-d{external_id}")

    category, subcategory, category_path = _categories(node)
    location, cities = _location(node)

    amount = node.get("price")
    price = Price(amount=amount if isinstance(amount, (int, float)) else None,
                  currency="DZD" if isinstance(amount, (int, float)) and amount > 0 else None,
                  raw=_price_raw(node), unit=node.get("priceUnit"))

    attributes: dict[str, Any] = {}
    labels: dict[str, str] = {}
    for spec in node.get("specs") or []:
        meta = spec.get("specification") or {}
        key = meta.get("codename")
        value = spec.get("valueText") or spec.get("value")
        if key and value not in (None, [], ""):
            attributes[key] = value[0] if isinstance(value, list) and len(value) == 1 else value
            if meta.get("label"):
                labels[key] = meta["label"]
    for item in node.get("smallDescription") or []:  # search-summary form of the same specs
        key = (item.get("specification") or {}).get("codename")
        if key and item.get("valueText") not in (None, [], "") and key not in attributes:
            attributes[key] = item["valueText"]

    store = node.get("store") if isinstance(node.get("store"), dict) else None
    user = node.get("user") if isinstance(node.get("user"), dict) else None
    if node.get("isFromStore") and store and store.get("name"):
        seller = Seller(display_name=store["name"], type="store")
    elif user and user.get("displayName"):
        seller = Seller(display_name=user["displayName"], type="individual")
    else:
        seller = Seller(type="store" if node.get("isFromStore") else None)

    store_meta = _compact({
        "id": store.get("id"), "slug": store.get("slug"), "url": store.get("url"),
        "description": store.get("description"), "followers": store.get("followerCount"),
        "announcements_count": store.get("announcementsCount"), "status": store.get("status"),
        "is_official": store.get("isOfficial"), "is_verified": store.get("isVerified"),
        "addresses": [(l.get("location") or {}).get("address") for l in store.get("locations") or []],
        "categories": [c.get("name") for c in store.get("categories") or []],
    }) if store else {}

    category_obj = node.get("category") or {}
    metadata = _compact({
        "api": "ouedkniss-graphql", "reference": node.get("reference"), "slug": slug,
        "refreshed_at": node.get("refreshedAt"), "status": node.get("status"),
        "price_type": node.get("priceType"), "price_preview": node.get("pricePreview"),
        "old_price": node.get("oldPrice") or None, "exchange_type": node.get("exchangeType"),
        "has_delivery": node.get("hasDelivery"), "delivery_type": node.get("deliveryType"),
        "quantity": node.get("quantity"), "street_name": node.get("street_name"),
        "has_phone": node.get("hasPhone"), "has_email": node.get("hasEmail"),  # flags only
        "category_id": category_obj.get("id"), "category_slug": category_obj.get("slug"),
        "category_path": category_path,
        "category_ids": [c.get("id") for c in node.get("categories") or []],
        "cities": cities if len(cities) > 1 else None,
        "store": store_meta, "attribute_labels": labels,
        "variants": node.get("variants") or None,
        "is_detail_record": "specs" in node,
    })

    return Listing(
        external_id=external_id, url=url, title=node.get("title"),
        category=category, subcategory=subcategory, price=price, location=location,
        description=node.get("description"), published_at=node.get("createdAt"),
        seller=seller, attributes=attributes,
        images=parse_api_media(node.get("medias"), node.get("defaultMedia")),
        source_metadata=metadata, scraped_at=scraped_at or utc_now_iso(),
    )


# ===================== HTML strategies (fallback, isolated) =====================

def _first_text(soup: BeautifulSoup, selectors: list[str]) -> str | None:
    for sel in selectors:
        el = soup.select_one(sel)
        if el:
            text = el.get("content") or el.get("datetime") or el.get_text(" ", strip=True)
            if text:
                return str(text).strip()
    return None


def _strategy_json_ld(soup: BeautifulSoup) -> dict[str, Any]:
    obj = find_type(extract_json_ld(soup), "Product", "Offer", "Vehicle", "RealEstateListing",
                    "Residence", "Apartment", "House", "Car", "Service")
    if not obj:
        return {}
    offers = obj.get("offers")
    offers = offers[0] if isinstance(offers, list) and offers else offers
    offers = offers if isinstance(offers, dict) else {}
    seller = offers.get("seller") or obj.get("seller") or {}
    image = obj.get("image")
    images = [i if isinstance(i, str) else (i or {}).get("url") for i in (image if isinstance(image, list) else [image])]
    price = offers.get("price")
    currency = offers.get("priceCurrency")
    return {
        "title": obj.get("name"), "description": obj.get("description"),
        "price_raw": f"{price} {currency or ''}".strip() if price not in (None, "") else None,
        "category": obj.get("category"),
        "images": [i for i in images if i],
        "seller": seller.get("name") if isinstance(seller, dict) else None,
        "published_at": obj.get("datePosted") or obj.get("datePublished"),
        "location": ((offers.get("availableAtOrFrom") or {}).get("name")
                     if isinstance(offers.get("availableAtOrFrom"), dict) else None),
        "url": obj.get("url"),
    }


def _strategy_semantic(soup: BeautifulSoup) -> dict[str, Any]:
    def meta(name: str) -> str | None:
        el = soup.select_one(f'meta[property="{name}"], meta[name="{name}"]')
        return el.get("content") if el else None

    h1 = soup.find("h1")
    canonical = soup.select_one('link[rel="canonical"]')
    time_el = soup.select_one("time[datetime]")
    price_el = soup.select_one("[itemprop=price]")
    return {
        "title": (h1.get_text(" ", strip=True) if h1 else None) or meta("og:title"),
        "description": meta("og:description") or meta("description"),
        "url": canonical.get("href") if canonical else meta("og:url"),
        "published_at": time_el.get("datetime") if time_el else None,
        "price_raw": (price_el.get("content") or price_el.get_text(strip=True)) if price_el else None,
    }


def _strategy_css(soup: BeautifulSoup) -> dict[str, Any]:
    return {
        "title": _first_text(soup, SELECTORS["title"]),
        "price_raw": _first_text(soup, SELECTORS["price"]),
        "description": _first_text(soup, SELECTORS["description"]),
        "location": _first_text(soup, SELECTORS["location"]),
        "seller": _first_text(soup, SELECTORS["seller"]),
        "published_at": _first_text(soup, SELECTORS["published_at"]),
    }


def parse_html_listing(html: str, page_url: str, base_url: str = "https://www.ouedkniss.com",
                       scraped_at: str | None = None) -> Listing | None:
    """Parse a rendered listing page. Returns None when no listing can be identified."""
    soup = BeautifulSoup(html, "lxml")

    for blob in extract_embedded_json(soup):  # strategy 1: complete embedded record
        node = find_announcement(blob)
        if node:
            return parse_api_announcement(node, base_url, scraped_at)

    ld, sem, css = _strategy_json_ld(soup), _strategy_semantic(soup), _strategy_css(soup)

    def pick(key: str) -> Any:
        for source in (ld, sem, css):
            if source.get(key):
                return source[key]
        return None

    url = normalize_url(pick("url") or page_url)
    external_id = extract_external_id(url) or extract_external_id(page_url)
    if not external_id:
        return None  # never invent an id

    return Listing(
        external_id=external_id, url=url, title=pick("title"), category=pick("category"),
        price=Price(raw=pick("price_raw")),
        location=Location(raw=pick("location")),
        description=pick("description"), published_at=pick("published_at"),
        seller=Seller(display_name=pick("seller")),
        images=parse_html_images(soup, base_url, ld.get("images")),
        source_metadata={"api": "html", "strategies": [n for n, s in
                                                      (("json_ld", ld), ("semantic", sem), ("css", css)) if any(s.values())]},
        scraped_at=scraped_at or utc_now_iso(),
    )

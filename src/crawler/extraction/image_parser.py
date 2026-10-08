"""Image extraction. Only URLs are collected; binaries are never fetched here."""

from __future__ import annotations

from typing import Any

from bs4 import BeautifulSoup

from crawler.models.image import Image
from crawler.utils.urls import normalize_image_url


def _to_int(value: Any) -> int | None:
    try:
        n = int(str(value).strip().rstrip("px"))
        return n if n > 0 else None
    except (TypeError, ValueError):
        return None


def parse_api_media(medias: list[dict[str, Any]] | None,
                    default_media: dict[str, Any] | None = None) -> list[Image]:
    """Images from the API's `medias` list (falls back to `defaultMedia`). Non-image media
    (videos) are skipped. Order is preserved, duplicates removed, positions 0-based."""
    candidates = [m for m in (medias or []) if isinstance(m, dict)]
    if not candidates and isinstance(default_media, dict):
        candidates = [default_media]
    return build_images((m.get("mediaUrl"), m.get("mimeType")) for m in candidates)


def build_images(pairs: Any, source: str = "listing", base_url: str | None = None) -> list[Image]:
    seen: set[str] = set()
    images: list[Image] = []
    for item in pairs:
        url, mime = item[0], item[1]
        if mime and not str(mime).lower().startswith("image"):
            continue
        norm = normalize_image_url(url, base_url)
        if not norm or norm in seen:
            continue
        seen.add(norm)
        images.append(Image(url=norm, position=len(images), source=source))
    return images


def parse_html_images(soup: BeautifulSoup, base_url: str | None = None,
                      extra_urls: list[str] | None = None) -> list[Image]:
    """Images from rendered HTML: og:image, JSON-LD image URLs (`extra_urls`), then <img> tags."""
    seen: set[str] = set()
    images: list[Image] = []

    def add(url: str | None, alt: str | None = None, width: Any = None, height: Any = None) -> None:
        norm = normalize_image_url(url, base_url)
        if not norm or norm in seen:
            return
        seen.add(norm)
        images.append(Image(url=norm, position=len(images), alt=(alt or "").strip() or None,
                            width=_to_int(width), height=_to_int(height), source="listing"))

    for meta in soup.select('meta[property="og:image"], meta[name="twitter:image"]'):
        add(meta.get("content"))
    for url in extra_urls or []:
        add(url)
    for img in soup.select("img"):
        src = img.get("src") or img.get("data-src") or img.get("data-lazy-src")
        if src:
            add(src, img.get("alt"), img.get("width"), img.get("height"))
    return images

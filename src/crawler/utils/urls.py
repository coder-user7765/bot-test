from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlencode, urljoin, urlsplit, urlunsplit

_TRACKING_PREFIXES = ("utm_",)
_TRACKING_KEYS = {"fbclid", "gclid", "msclkid", "agent", "agent_force", "lang"}
_ID_RE = re.compile(r"-d(\d+)(?:[/?#]|$)")


def normalize_url(url: str) -> str:
    """Canonical form used for deduplication: lower-case host, no fragment, no tracking
    params, sorted query, no trailing slash, https."""
    parts = urlsplit(url.strip())
    scheme = "https" if parts.scheme in ("", "http", "https") else parts.scheme
    netloc = parts.netloc.lower()
    path = re.sub(r"/{2,}", "/", parts.path).rstrip("/") or "/"
    query = sorted(
        (k, v) for k, v in parse_qsl(parts.query, keep_blank_values=True)
        if k.lower() not in _TRACKING_KEYS and not k.lower().startswith(_TRACKING_PREFIXES)
    )
    return urlunsplit((scheme, netloc, path, urlencode(query), ""))


def listing_url(base_url: str, slug: str, external_id: str) -> str:
    """Public listing URL pattern of the site: /<slug>-d<id>."""
    return normalize_url(f"{base_url.rstrip('/')}/{slug}-d{external_id}")


def extract_external_id(url: str) -> str | None:
    match = _ID_RE.search(urlsplit(url).path + "/")
    return match.group(1) if match else None


def normalize_image_url(url: str | None, base_url: str | None = None) -> str | None:
    """Absolute https URL without fragment; None if unusable (data: URIs, empty)."""
    if not url:
        return None
    url = url.strip()
    if not url or url.startswith("data:"):
        return None
    if url.startswith("//"):
        url = "https:" + url
    elif base_url and not re.match(r"^[a-zA-Z][a-zA-Z0-9+.-]*:", url):
        url = urljoin(base_url, url)
    parts = urlsplit(url)
    if parts.scheme not in ("http", "https") or not parts.netloc:
        return None
    return urlunsplit(("https", parts.netloc.lower(), parts.path, parts.query, ""))

"""Turns search-result nodes into DiscoveredListing records (id + public URL + summary)."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from crawler.utils.urls import listing_url


@dataclass(slots=True)
class DiscoveredListing:
    external_id: str
    url: str
    summary: dict[str, Any] = field(default_factory=dict)


def to_discovered(nodes: list[dict[str, Any]], base_url: str) -> list[DiscoveredListing]:
    out: dict[str, DiscoveredListing] = {}
    for node in nodes:
        if not isinstance(node, dict) or not node.get("id") or not node.get("slug"):
            continue
        ext_id = str(node["id"])
        out.setdefault(ext_id, DiscoveredListing(ext_id, listing_url(base_url, node["slug"], ext_id), node))
    return list(out.values())

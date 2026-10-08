"""Category -> pages. One call fetches one page of public search results."""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from crawler.http.client import HttpClient
from crawler.http.queries import SEARCH_QUERY


@dataclass(slots=True)
class SearchPage:
    nodes: list[dict[str, Any]] = field(default_factory=list)
    has_more: bool = False
    last_page: int | None = None
    total: int | None = None


async def fetch_search_page(client: HttpClient, category_slug: str, page: int,
                            page_size: int) -> SearchPage:
    if client.using_browser:
        search_url = f"{client.cfg.base_url.rstrip('/')}/{category_slug}"
        await client.get_text(search_url, referer=client.cfg.base_url)
        client.last_search_url = search_url
    data = await client.graphql("SearchAnnouncementsQuery", SEARCH_QUERY, {
        "q": None,
        "filter": {"categorySlug": category_slug, "page": page, "count": page_size,
                   "orderByField": {"field": "REFRESHED_AT"}},
    }, after_page=True)
    block = ((data.get("search") or {}).get("announcements")) or {}
    nodes = [n for n in (block.get("data") or []) if isinstance(n, dict) and n.get("id")]
    info = block.get("paginatorInfo") or {}
    return SearchPage(nodes=nodes, has_more=bool(info.get("hasMorePages")) and bool(nodes),
                      last_page=info.get("lastPage"), total=info.get("total"))

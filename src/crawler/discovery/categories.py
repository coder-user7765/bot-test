"""Public category discovery from the site's own navigation menu."""

from __future__ import annotations

from crawler.http.client import HttpClient
from crawler.http.queries import CATEGORY_CHILDREN_QUERY, MENU_QUERY
from crawler.logging_config import log_event


async def discover_categories(client: HttpClient, depth: int = 1,
                              explicit: list[str] | None = None) -> list[str]:
    """Category slugs to crawl.

    explicit  -> used as given.
    depth 1   -> top-level categories from the public menu.
    depth 2   -> the direct sub-categories of each top-level category (a top-level category
                 without children is kept as is).
    """
    if explicit:
        return list(dict.fromkeys(explicit))

    data = await client.graphql("listingMenu", MENU_QUERY, {"menuFilter": {}})
    top: list[str] = []
    for item in data.get("listingMenu") or []:
        target = item.get("target") or {}
        slug = target.get("slug")  # entries such as "Boutiques" are plain links -> no slug
        if slug and slug not in top:
            top.append(slug)
    log_event("categories_discovered", level=20, count=len(top), depth=depth)
    if depth <= 1:
        return top

    result: list[str] = []
    for slug in top:
        data = await client.graphql("CategoryChildren", CATEGORY_CHILDREN_QUERY, {
            "q": None, "filter": {"categorySlug": slug, "page": 1, "count": 1}})
        category = ((data.get("search") or {}).get("active") or {}).get("category") or {}
        children = [c["slug"] for c in category.get("children") or [] if c.get("slug")]
        result.extend(children or [slug])
    return list(dict.fromkeys(result))

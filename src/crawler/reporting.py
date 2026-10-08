"""Data-quality statistics over the stored listings."""

from __future__ import annotations

from pathlib import Path
from typing import Any

import orjson

from crawler.storage.database import Database


def quality_stats(db: Database) -> dict[str, Any]:
    keys = ("title", "price", "description", "images", "location", "seller", "category", "published_at")
    counts = dict.fromkeys(keys, 0)
    total = 0
    for l in db.iter_listings():
        total += 1
        counts["title"] += bool(l.get("title"))
        counts["price"] += (l.get("price") or {}).get("amount") is not None
        counts["description"] += bool(l.get("description"))
        counts["images"] += bool(l.get("images"))
        loc = l.get("location") or {}
        counts["location"] += bool(loc.get("wilaya") or loc.get("commune") or loc.get("raw"))
        counts["seller"] += bool((l.get("seller") or {}).get("display_name"))
        counts["category"] += bool(l.get("category"))
        counts["published_at"] += bool(l.get("published_at"))
    c = db.counts()
    return {
        "total_listings": total,
        "with_title": counts["title"], "with_price": counts["price"],
        "with_description": counts["description"], "with_images": counts["images"],
        "with_location": counts["location"], "with_seller": counts["seller"],
        "with_category": counts["category"], "with_published_at": counts["published_at"],
        "duplicates": c["duplicates"], "failed": c["failed"],
        "pending": c["pending"], "discovered": c["discovered"],
    }


def write_quality_report(db: Database, exports_dir: Path) -> dict[str, Any]:
    stats = quality_stats(db)
    exports_dir.mkdir(parents=True, exist_ok=True)
    (exports_dir / "data-quality.json").write_bytes(orjson.dumps(stats, option=orjson.OPT_INDENT_2) + b"\n")
    return stats

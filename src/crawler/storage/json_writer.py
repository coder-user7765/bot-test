"""JSONL / JSON output. JSONL (UTF-8, ensure_ascii=false) is the authoritative format."""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Iterable, Iterator

import orjson

from crawler.storage.database import Database


def dumps(obj: Any) -> bytes:
    return orjson.dumps(obj, option=orjson.OPT_NON_STR_KEYS)  # orjson never escapes non-ASCII


def image_records(listing: dict[str, Any]) -> Iterator[dict[str, Any]]:
    for img in listing.get("images") or []:
        yield {"external_id": listing["external_id"], "listing_url": listing["url"], **img}


class JsonlAppender:
    """Append-only writer used during the crawl; flushed + fsynced per record."""

    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path

    def append(self, records: Iterable[dict[str, Any]]) -> None:
        payload = b"".join(dumps(r) + b"\n" for r in records)
        if not payload:
            return
        with open(self.path, "ab") as fh:
            fh.write(payload)
            fh.flush()
            os.fsync(fh.fileno())


def _atomic_write(path: Path, chunks: Iterable[bytes]) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with open(tmp, "wb") as fh:
        for chunk in chunks:
            fh.write(chunk)
        fh.flush()
        os.fsync(fh.fileno())
    os.replace(tmp, path)


def export_all(db: Database, normalized_dir: Path, summary_dir: Path) -> dict[str, Any]:
    """Rebuild listings.jsonl / listings.json / images.jsonl from SQLite (the source of truth,
    already de-duplicated) and write summary.json. Files are replaced atomically."""
    normalized_dir.mkdir(parents=True, exist_ok=True)
    summary_dir.mkdir(parents=True, exist_ok=True)
    from crawler.reporting import quality_stats  # local import: avoids a cycle

    _atomic_write(normalized_dir / "listings.jsonl", (dumps(l) + b"\n" for l in db.iter_listings()))

    def json_array() -> Iterator[bytes]:
        yield b"[\n"
        first = True
        for l in db.iter_listings():
            yield (b"" if first else b",\n") + dumps(l)
            first = False
        yield b"\n]\n"

    _atomic_write(normalized_dir / "listings.json", json_array())
    _atomic_write(normalized_dir / "images.jsonl",
                  (dumps(r) + b"\n" for l in db.iter_listings() for r in image_records(l)))

    stats = quality_stats(db)
    summary = {
        "total_listings": stats["total_listings"], "with_price": stats["with_price"],
        "with_images": stats["with_images"], "with_description": stats["with_description"],
        "with_location": stats["with_location"], "duplicates": stats["duplicates"],
        "failed": stats["failed"],
    }
    _atomic_write(summary_dir / "summary.json", [orjson.dumps(summary, option=orjson.OPT_INDENT_2) + b"\n"])
    return summary

"""Per-listing processing: fetch detail -> parse -> normalise -> dedupe/persist -> JSONL."""

from __future__ import annotations

import asyncio
import logging
import threading
import time
import traceback
from dataclasses import dataclass, field
from typing import Any

import orjson

from crawler.config import CrawlerConfig
from crawler.extraction.listing_parser import parse_api_announcement
from crawler.http.client import CrawlAborted, FetchError, HttpClient
from crawler.http.queries import DETAIL_QUERY
from crawler.logging_config import log_event
from crawler.normalization.listing_normalizer import normalize_listing
from crawler.storage.database import Database
from crawler.storage.json_writer import JsonlAppender, image_records


@dataclass
class Stats:
    started: float = field(default_factory=time.monotonic)
    pages: int = 0
    new_urls: int = 0
    success: int = 0
    skipped: int = 0
    failed: int = 0

    @property
    def elapsed(self) -> float:
        return time.monotonic() - self.started


@dataclass
class RunContext:
    cfg: CrawlerConfig
    db: Database
    client: HttpClient
    stats: Stats
    stop: threading.Event
    listings_out: JsonlAppender
    images_out: JsonlAppender
    aborted: CrawlAborted | None = None
    halt: bool = False

    def limit_hit(self) -> bool:
        return bool(self.cfg.max_listings) and self.stats.success >= self.cfg.max_listings

    def should_stop(self) -> bool:
        return self.halt or self.stop.is_set() or self.limit_hit()


async def process_listing(ctx: RunContext, row: Any) -> None:
    cfg, db = ctx.cfg, ctx.db
    url, ext_id = row["url"], row["external_id"]
    db.mark_processing(url)
    try:
        if cfg.fetch_details:
            if ctx.client.using_browser:
                await ctx.client.get_text(url, referer=ctx.client.last_search_url)
            data = await ctx.client.graphql("AnnouncementGet", DETAIL_QUERY, {"id": ext_id},
                                            after_page=True)
            node = data.get("announcement")
            if not node:
                db.mark_failed(url, "not_found: listing no longer public", 200)
                ctx.stats.failed += 1
                log_event("not_found", url=url, status=200)
                return
            if cfg.save_raw:
                cfg.raw_dir.mkdir(parents=True, exist_ok=True)
                (cfg.raw_dir / f"{ext_id}.json").write_bytes(orjson.dumps(node, option=orjson.OPT_INDENT_2))
        else:
            node = orjson.loads(row["summary_json"])

        listing = normalize_listing(parse_api_announcement(node, cfg.base_url))
        outcome = db.save_listing(listing, 200)  # durable before anything else happens
        if outcome in ("inserted", "updated"):
            dumped = listing.model_dump(mode="json")
            ctx.listings_out.append([dumped])
            ctx.images_out.append(image_records(dumped))
            if cfg.download_images:
                await download_images(ctx, listing)
        if outcome == "duplicate":
            ctx.stats.skipped += 1
        else:
            ctx.stats.success += 1
        log_event("listing_saved", url=url, status=200, outcome=outcome)
    except CrawlAborted:
        db.release(url)  # not this listing's fault: put it back in the queue, attempt not counted
        raise
    except FetchError as exc:
        retry = exc.retryable and db.attempts(url) < cfg.max_url_attempts
        if retry:
            db.release(url, str(exc), exc.status, count_attempt=True)
            log_event("listing_retry_later", level=logging.WARNING, url=url, status=exc.status, error=str(exc))
        else:
            db.mark_failed(url, str(exc), exc.status)
            ctx.stats.failed += 1
            log_event("listing_failed", level=logging.ERROR, url=url, status=exc.status, error=str(exc))
    except Exception as exc:  # parser bug etc.: record it, keep crawling
        db.mark_failed(url, f"{type(exc).__name__}: {exc}")
        ctx.stats.failed += 1
        log_event("listing_error", level=logging.ERROR, url=url, error=f"{type(exc).__name__}: {exc}")
        logging.getLogger("crawler").debug(traceback.format_exc())


async def download_images(ctx: RunContext, listing: Any) -> None:
    """Optional, conservative: same rate limiter, robots check, failures are non-fatal."""
    folder = ctx.cfg.images_dir / listing.external_id
    for img in listing.images:
        suffix = img.url.split("?")[0].rsplit(".", 1)[-1].lower()
        suffix = suffix if suffix in {"jpg", "jpeg", "png", "webp", "gif"} else "jpg"
        target = folder / f"{img.position:02d}.{suffix}"
        if target.exists():
            continue
        try:
            content = await ctx.client.get_bytes(img.url)
        except FetchError as exc:
            log_event("image_failed", level=logging.WARNING, url=img.url, status=exc.status, error=str(exc))
            continue
        folder.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content)


async def run_workers(ctx: RunContext, rows: list[Any], concurrency: int) -> None:
    queue: asyncio.Queue[Any] = asyncio.Queue()
    for row in rows:
        queue.put_nowait(row)

    async def worker() -> None:
        while not ctx.should_stop():
            try:
                row = queue.get_nowait()
            except asyncio.QueueEmpty:
                return
            try:
                await process_listing(ctx, row)
            except CrawlAborted as exc:
                ctx.aborted = ctx.aborted or exc
                ctx.halt = True
                return

    await asyncio.gather(*(worker() for _ in range(max(1, concurrency))))

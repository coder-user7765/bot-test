"""Orchestration: Category -> Pagination -> Listing URLs -> (queue) -> workers."""

from __future__ import annotations

import logging
import threading
from dataclasses import dataclass

from crawler.config import CrawlerConfig
from crawler.crawler.workers import RunContext, Stats, run_workers
from crawler.discovery.categories import discover_categories
from crawler.discovery.listings import to_discovered
from crawler.discovery.pagination import fetch_search_page
from crawler.http.client import (BlockedError, CrawlAborted, FetchError, HttpClient,
                                 LimitReached, StopRequested)
from crawler.logging_config import log_event
from crawler.storage.checkpoint import Checkpoint
from crawler.storage.database import Database
from crawler.storage.json_writer import JsonlAppender

MESSAGES = {
    "completed": "All configured categories are fully crawled and the queue is empty.",
    "limit_reached": "A configured limit was reached. Raise it and run `resume` to continue.",
    "interrupted": "Stopped on request. All progress is saved; run `python -m crawler resume`.",
}


@dataclass
class RunResult:
    reason: str
    message: str


class Coordinator:
    def __init__(self, cfg: CrawlerConfig, db: Database, client: HttpClient,
                 stats: Stats, stop: threading.Event) -> None:
        self.cfg, self.db, self.client, self.stats, self.stop = cfg, db, client, stats, stop
        self.checkpoint = Checkpoint(db)
        self.ctx = RunContext(
            cfg=cfg, db=db, client=client, stats=stats, stop=stop,
            listings_out=JsonlAppender(cfg.normalized_dir / "listings.jsonl"),
            images_out=JsonlAppender(cfg.normalized_dir / "images.jsonl"))

    # ---- limits ------------------------------------------------------
    def _pages_limit_hit(self) -> bool:
        return bool(self.cfg.max_pages) and self.stats.pages >= self.cfg.max_pages

    def _check_stop(self) -> None:
        if self.stop.is_set():
            raise StopRequested(MESSAGES["interrupted"])
        if self.ctx.limit_hit():
            raise LimitReached("max_listings reached")

    # ---- main --------------------------------------------------------
    async def run(self, restart_categories: bool = False) -> RunResult:
        recovered = self.db.recover_interrupted()
        if recovered:
            log_event("recovered_interrupted", count=recovered)
        try:
            await self._drain()  # finish whatever a previous run left in the queue
            slugs = await self._categories()
            if restart_categories:
                for slug in slugs:
                    self.checkpoint.reopen(slug)
            for slug in slugs:
                if self.checkpoint.is_done(slug):
                    continue
                if self._pages_limit_hit():
                    raise LimitReached("max_pages reached")
                await self._crawl_category(slug)
            reason = "completed"
            result = RunResult(reason, MESSAGES[reason])
        except BlockedError as exc:
            result = RunResult(exc.reason, str(exc))
        except CrawlAborted as exc:
            result = RunResult(exc.reason, MESSAGES.get(exc.reason, str(exc)) if exc.reason != "connection_failure" else str(exc))
        except FetchError as exc:  # search page / menu failed for good: stop cleanly, state is saved
            log_event("run_error", level=logging.ERROR, status=exc.status, error=str(exc))
            result = RunResult("error", f"Stopped after a request failed: {exc}. Run `resume` to try again.")
        self.checkpoint.note_run(result.reason)
        log_event("run_finished", reason=result.reason, requests=self.client.requests,
                  success=self.stats.success, pages=self.stats.pages)
        return result

    async def _categories(self) -> list[str]:
        if self.cfg.categories:
            slugs = list(dict.fromkeys(self.cfg.categories))
        else:
            slugs = self.checkpoint.categories()
            if not slugs:
                slugs = await discover_categories(self.client, self.cfg.category_depth)
        self.checkpoint.save_categories(slugs)
        return slugs

    async def _crawl_category(self, slug: str) -> None:
        log_event("category_start", category=slug, page=self.checkpoint.next_page(slug))
        while True:
            self._check_stop()
            page = self.checkpoint.next_page(slug)
            per_cat = self.cfg.max_pages_per_category
            if per_cat and page > per_cat:
                self.checkpoint.mark_done(slug)
                return
            if self._pages_limit_hit():
                raise LimitReached("max_pages reached")

            result = await fetch_search_page(self.client, slug, page, self.cfg.page_size)
            self.stats.pages += 1
            items = to_discovered(result.nodes, self.cfg.base_url)
            new = self.db.add_discovered(items, slug, (self.checkpoint.cursor_key(slug), str(page + 1)))
            self.stats.new_urls += new
            log_event("page_discovered", category=slug, page=page, found=len(items), new=new,
                      last_page=result.last_page)
            if not result.has_more:
                self.checkpoint.mark_done(slug)
            await self._drain()
            if not result.has_more:
                return

    async def _drain(self) -> None:
        """Process queued listings until the queue is empty, a limit is hit, or we must stop."""
        while True:
            self._check_stop()
            left = (self.cfg.max_listings - self.stats.success) if self.cfg.max_listings else 200
            rows = self.db.pending(max(1, min(left, 200)))
            if not rows:
                return
            await run_workers(self.ctx, rows, self.cfg.max_concurrent_requests)
            if self.ctx.aborted:
                raise self.ctx.aborted

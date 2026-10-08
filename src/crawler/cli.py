"""Command-line interface (argparse) with Rich output."""

from __future__ import annotations

import argparse
import asyncio
import signal
import sys
import threading
import time
from pathlib import Path

from rich.console import Console
from rich.live import Live
from rich.table import Table

from crawler import __version__
from crawler.config import CrawlerConfig, load_config
from crawler.crawler.coordinator import Coordinator
from crawler.crawler.workers import Stats
from crawler.http.client import BLOCK_MESSAGE, HttpClient
from crawler.logging_config import log_event, setup_logging
from crawler.reporting import write_quality_report
from crawler.storage.checkpoint import Checkpoint
from crawler.storage.database import Database
from crawler.storage.json_writer import export_all

console = Console()

EXIT_OK, EXIT_ERROR, EXIT_BLOCKED, EXIT_INTERRUPTED = 0, 1, 3, 130


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m crawler", description="ouedkniss-data-lab")
    parser.add_argument("--config", type=Path, help="path to crawler.yaml")
    parser.add_argument("--version", action="version", version=__version__)
    sub = parser.add_subparsers(dest="command", required=True)

    for name, helptext in (("crawl", "discover and fetch listings (continues from saved state)"),
                           ("resume", "continue exactly where the last run stopped")):
        p = sub.add_parser(name, help=helptext)
        p.add_argument("--max-pages", type=int, help="search pages to fetch in this run (0 = unlimited)")
        p.add_argument("--max-listings", type=int, help="listings to save in this run (0 = unlimited)")
        p.add_argument("--max-requests", type=int, help="HTTP requests in this run (0 = unlimited)")
        p.add_argument("--delay", type=float, help="seconds between requests")
        p.add_argument("--concurrency", type=int, help="max concurrent requests (1-4)")
        p.add_argument("--category", action="append", help="category slug (repeatable); default: auto-discover")
        p.add_argument("--retry-failed", action="store_true", help="re-queue FAILED listings first")
        p.add_argument("--restart-categories", action="store_true",
                       help="re-scan finished categories from page 1 (picks up new listings)")

    sub.add_parser("stop", help="ask a running (e.g. detached) crawl to stop gracefully")
    sub.add_parser("status", help="show crawl progress")
    sub.add_parser("export", help="write listings.jsonl, listings.json, images.jsonl, summary.json")
    sub.add_parser("report", help="data-quality report (also written to data/exports/data-quality.json)")
    sub.add_parser("categories", help="list the categories the crawler would use")
    p = sub.add_parser("failed", help="list failed listings")
    p.add_argument("--limit", type=int, default=50)
    p.add_argument("--requeue", action="store_true", help="put all FAILED listings back in the queue")
    p = sub.add_parser("reset", help="delete all crawl state and collected data")
    p.add_argument("--yes", action="store_true", help="do not ask for confirmation")
    return parser


# ---------------------------------------------------------------- dashboard
def _counts_table(db: Database, stats: Stats | None, client: HttpClient | None) -> Table:
    c = db.counts(full=False)
    table = Table(title="ouedkniss-data-lab", show_header=False, expand=False)
    table.add_column(style="bold cyan")
    table.add_column(justify="right")
    for label, key in (("Discovered", "discovered"), ("Processed", "processed"), ("Success", "success"),
                       ("Failed", "failed"), ("Pending", "pending")):
        table.add_row(label, f"{c[key]:,}")
    if stats and client:
        elapsed = max(stats.elapsed, 0.001)
        table.add_row("Requests/sec", f"{client.requests / elapsed:.2f}")
        table.add_row("Run: pages / saved", f"{stats.pages} / {stats.success}")
        table.add_row("Elapsed", time.strftime("%H:%M:%S", time.gmtime(elapsed)))
        table.add_row("Current URL", (client.current_url or "-")[-70:])
    return table


async def _dashboard(db: Database, stats: Stats, client: HttpClient) -> None:
    if console.is_terminal:
        with Live(_counts_table(db, stats, client), console=console, refresh_per_second=2) as live:
            while True:
                live.update(_counts_table(db, stats, client))
                await asyncio.sleep(1)
    else:  # scheduled / redirected output: one plain line per minute
        while True:
            await asyncio.sleep(60)
            c = db.counts(full=False)
            console.print(f"[{time.strftime('%H:%M:%S')}] discovered={c['discovered']} processed={c['processed']} "
                          f"success={c['success']} failed={c['failed']} pending={c['pending']} "
                          f"req/s={client.requests / max(stats.elapsed, 0.001):.2f}", highlight=False)


async def _watch_stop_file(cfg: CrawlerConfig, stop: threading.Event) -> None:
    """`python -m crawler stop` drops data/STOP; a detached run (no console) stops gracefully."""
    flag = cfg.data_path / "STOP"
    while True:
        if flag.exists():
            flag.unlink(missing_ok=True)
            log_event("stop_file_seen")
            stop.set()
        await asyncio.sleep(2)


def _install_signal_handlers(stop: threading.Event) -> None:
    def handler(signum: int, frame: object) -> None:
        if stop.is_set():
            raise KeyboardInterrupt  # second Ctrl+C: stop immediately (state is still consistent)
        stop.set()
        console.print("\n[yellow]Stopping after in-flight requests... progress is saved. "
                      "(Ctrl+C again to force)[/yellow]")

    for name in ("SIGINT", "SIGTERM", "SIGBREAK"):  # SIGBREAK: Windows Ctrl+Break / console close
        sig = getattr(signal, name, None)
        if sig is not None:
            try:
                signal.signal(sig, handler)
            except (ValueError, OSError):
                pass


def _print_resume_point(db: Database) -> None:
    """Tell the user where this run picks up (everything comes from SQLite)."""
    c = db.counts(full=False)
    if not c["discovered"]:
        console.print("No saved state: starting a fresh crawl.")
        return
    cp = Checkpoint(db)
    cats = cp.categories() or []
    nxt = next((s for s in cats if not cp.is_done(s)), None)
    where = f"category '{nxt}' page {cp.next_page(nxt)}" if nxt else "no category left to scan"
    console.print(f"Resuming from saved state: {c['success']:,} saved, {c['pending']:,} queued, "
                  f"{c['failed']:,} failed; categories done {sum(cp.is_done(s) for s in cats)}/{len(cats)}; "
                  f"next: {where}.")


# ---------------------------------------------------------------- commands
def cmd_crawl(cfg: CrawlerConfig, args: argparse.Namespace) -> int:
    db = Database(cfg.db_path)
    stop = threading.Event()
    _install_signal_handlers(stop)
    (cfg.data_path / "STOP").unlink(missing_ok=True)  # ignore a stale stop request
    if args.retry_failed:
        console.print(f"Re-queued {db.requeue_failed()} failed listings.")

    async def run() -> tuple[str, str, Stats, HttpClient]:
        stats = Stats()
        async with HttpClient(cfg, stop) as client:
            coord = Coordinator(cfg, db, client, stats, stop)
            dash = asyncio.create_task(_dashboard(db, stats, client))
            watcher = asyncio.create_task(_watch_stop_file(cfg, stop))
            try:
                result = await coord.run(restart_categories=args.restart_categories)
            finally:
                for task in (dash, watcher):
                    task.cancel()
                await asyncio.gather(dash, watcher, return_exceptions=True)
            return result.reason, result.message, stats, client

    _print_resume_point(db)
    console.print(f"Starting crawl (limits per run: listings={cfg.max_listings or '∞'}, "
                  f"pages={cfg.max_pages or '∞'}, requests={cfg.max_requests or '∞'}; "
                  f"delay={cfg.request_delay_seconds}s, concurrency={cfg.max_concurrent_requests})")
    try:
        reason, message, stats, client = asyncio.run(run())
    except KeyboardInterrupt:
        console.print("[red]Forced stop. State is safe; in-flight listings are re-queued on the next run.[/red]")
        return EXIT_INTERRUPTED
    finally:
        db.close()

    summary_db = Database(cfg.db_path)
    try:
        console.print(_counts_table(summary_db, stats, client))
        # rebuild the JSONL/JSON files from SQLite so they match the saved state after any stop
        try:
            export_all(summary_db, cfg.normalized_dir, cfg.exports_dir)
        except OSError as exc:  # Windows: file open in Excel/Notepad. Data is safe in SQLite.
            log_event("export_failed", error=str(exc))
            console.print(f"[yellow]Could not rewrite output files ({exc}). Close them and run "
                          "`python -m crawler export`.[/yellow]")
    finally:
        summary_db.close()
    if reason == "blocked":
        console.print(f"[bold red]{message if BLOCK_MESSAGE in message else BLOCK_MESSAGE}[/bold red]")
        return EXIT_BLOCKED
    console.print(f"[green]{message}[/green]" if reason in ("completed", "limit_reached", "interrupted")
                  else f"[red]{message}[/red]")
    if reason == "interrupted":
        return EXIT_INTERRUPTED
    return EXIT_OK if reason in ("completed", "limit_reached") else EXIT_ERROR


def cmd_status(cfg: CrawlerConfig) -> int:
    db = Database(cfg.db_path)
    c = db.counts()
    console.print(_counts_table(db, None, None))
    console.print(f"Skipped (duplicates): {c['skipped']:,}   Stored listings: {c['listings']:,}")
    cp = Checkpoint(db)
    cats = cp.categories()
    if cats:
        done = sum(cp.is_done(s) for s in cats)
        console.print(f"Categories: {done}/{len(cats)} fully crawled")
    last = db.get_state("last_run")
    if last:
        console.print(f"Last run: {last}")
    db.close()
    return EXIT_OK


def cmd_export(cfg: CrawlerConfig) -> int:
    db = Database(cfg.db_path)
    summary = export_all(db, cfg.normalized_dir, cfg.exports_dir)
    db.close()
    console.print(f"Wrote {cfg.normalized_dir / 'listings.jsonl'} (+ listings.json, images.jsonl) "
                  f"and {cfg.exports_dir / 'summary.json'}")
    for k, v in summary.items():
        console.print(f"  {k}: {v:,}")
    return EXIT_OK


def cmd_report(cfg: CrawlerConfig) -> int:
    db = Database(cfg.db_path)
    stats = write_quality_report(db, cfg.exports_dir)
    db.close()
    table = Table(title="Data quality", show_header=False)
    labels = {"total_listings": "Total listings", "with_title": "Listings with title",
              "with_price": "Listings with price", "with_description": "Listings with description",
              "with_images": "Listings with images", "with_location": "Listings with location",
              "with_seller": "Listings with seller", "duplicates": "Duplicate count", "failed": "Failed count"}
    for key, label in labels.items():
        table.add_row(label, f"{stats[key]:,}")
    console.print(table)
    console.print(f"Written to {cfg.exports_dir / 'data-quality.json'}")
    return EXIT_OK


def cmd_failed(cfg: CrawlerConfig, args: argparse.Namespace) -> int:
    db = Database(cfg.db_path)
    if args.requeue:
        console.print(f"Re-queued {db.requeue_failed()} failed listings.")
    else:
        table = Table(title="Failed listings")
        for col in ("URL", "HTTP", "Attempts", "Error"):
            table.add_column(col, overflow="fold")
        for r in db.failed_rows(args.limit):
            table.add_row(r["url"], str(r["http_status"] or "-"), str(r["attempt_count"]), (r["error"] or "")[:120])
        console.print(table)
        console.print(f"Total failed: {db.counts(full=False)['failed']:,}")
    db.close()
    return EXIT_OK


def cmd_reset(cfg: CrawlerConfig, args: argparse.Namespace) -> int:
    if not args.yes and input("Delete ALL crawl state and collected data? Type 'yes': ").strip().lower() != "yes":
        console.print("Cancelled.")
        return EXIT_OK
    db = Database(cfg.db_path)
    db.reset()
    db.close()
    for name in ("listings.jsonl", "listings.json", "images.jsonl"):
        (cfg.normalized_dir / name).unlink(missing_ok=True)
    console.print("State and normalized outputs deleted (data/raw and data/images untouched).")
    return EXIT_OK


def cmd_stop(cfg: CrawlerConfig) -> int:
    (cfg.data_path / "STOP").write_text("stop\n", encoding="utf-8")
    console.print("Stop requested: a running crawl will finish in-flight requests and exit within seconds.")
    return EXIT_OK


def cmd_categories(cfg: CrawlerConfig) -> int:
    from crawler.discovery.categories import discover_categories

    async def run() -> list[str]:
        async with HttpClient(cfg) as client:
            return await discover_categories(client, cfg.category_depth, cfg.categories)

    for slug in asyncio.run(run()):
        console.print(slug)
    return EXIT_OK


def run_cli(argv: list[str]) -> int:
    args = build_parser().parse_args(argv)
    overrides: dict = {}
    if args.command in ("crawl", "resume"):
        overrides = {"max_pages": args.max_pages, "max_listings": args.max_listings,
                     "max_requests": args.max_requests, "request_delay_seconds": args.delay,
                     "max_concurrent_requests": args.concurrency, "categories": args.category}
    try:
        cfg = load_config(args.config, overrides)
    except Exception as exc:  # invalid YAML / value
        console.print(f"[red]Configuration error:[/red] {exc}")
        return EXIT_ERROR
    cfg.ensure_dirs()
    setup_logging(cfg.logs_dir / "crawler.log")
    log_event("command", command=args.command)

    try:
        match args.command:
            case "crawl" | "resume":
                return cmd_crawl(cfg, args)
            case "status":
                return cmd_status(cfg)
            case "export":
                return cmd_export(cfg)
            case "report":
                return cmd_report(cfg)
            case "failed":
                return cmd_failed(cfg, args)
            case "reset":
                return cmd_reset(cfg, args)
            case "stop":
                return cmd_stop(cfg)
            case "categories":
                return cmd_categories(cfg)
    except KeyboardInterrupt:
        return EXIT_INTERRUPTED
    return EXIT_ERROR

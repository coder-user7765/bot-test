"""SQLite state + data store. Every state change is its own committed transaction
(WAL + synchronous=FULL), so a crash, Ctrl+C or power loss never loses finished work."""

from __future__ import annotations

import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterator

import orjson

from crawler.models.listing import Listing

DISCOVERED, PROCESSING, SUCCESS, FAILED, SKIPPED = "DISCOVERED", "PROCESSING", "SUCCESS", "FAILED", "SKIPPED"

SCHEMA = """
CREATE TABLE IF NOT EXISTS discovered_urls (
    url             TEXT PRIMARY KEY,
    external_id     TEXT NOT NULL UNIQUE,
    status          TEXT NOT NULL DEFAULT 'DISCOVERED',
    attempt_count   INTEGER NOT NULL DEFAULT 0,
    first_seen_at   TEXT NOT NULL,
    last_attempt_at TEXT,
    last_success_at TEXT,
    http_status     INTEGER,
    error           TEXT,
    content_hash    TEXT,
    category_slug   TEXT,
    summary_json    TEXT
);
CREATE INDEX IF NOT EXISTS idx_discovered_status ON discovered_urls(status);

CREATE TABLE IF NOT EXISTS processed_urls (
    url          TEXT PRIMARY KEY,
    external_id  TEXT NOT NULL,
    status       TEXT NOT NULL,
    content_hash TEXT,
    http_status  INTEGER,
    processed_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS failed_urls (
    url           TEXT PRIMARY KEY,
    external_id   TEXT,
    attempt_count INTEGER NOT NULL,
    http_status   INTEGER,
    error         TEXT,
    failed_at     TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS crawler_state (
    key        TEXT PRIMARY KEY,
    value      TEXT NOT NULL,
    updated_at TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS listings (
    external_id  TEXT PRIMARY KEY,
    url          TEXT NOT NULL UNIQUE,
    content_hash TEXT,
    data_json    TEXT NOT NULL,
    scraped_at   TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS idx_listings_hash ON listings(content_hash);
"""


def now_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class Database:
    def __init__(self, path: Path) -> None:
        path.parent.mkdir(parents=True, exist_ok=True)
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30, isolation_level=None)  # explicit transactions
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA journal_mode=WAL")
        self.conn.execute("PRAGMA synchronous=FULL")
        self.conn.execute("PRAGMA busy_timeout=30000")
        self.conn.executescript(SCHEMA)

    def close(self) -> None:
        self.conn.close()

    @contextmanager
    def transaction(self) -> Iterator[sqlite3.Connection]:
        self.conn.execute("BEGIN IMMEDIATE")
        try:
            yield self.conn
        except BaseException:
            self.conn.execute("ROLLBACK")
            raise
        else:
            self.conn.execute("COMMIT")

    # ---- crawler_state ------------------------------------------------
    def get_state(self, key: str, default: str | None = None) -> str | None:
        row = self.conn.execute("SELECT value FROM crawler_state WHERE key=?", (key,)).fetchone()
        return row["value"] if row else default

    def set_state(self, key: str, value: str, conn: sqlite3.Connection | None = None) -> None:
        sql = ("INSERT INTO crawler_state(key,value,updated_at) VALUES(?,?,?) "
               "ON CONFLICT(key) DO UPDATE SET value=excluded.value, updated_at=excluded.updated_at")
        if conn is not None:
            conn.execute(sql, (key, value, now_iso()))
        else:
            with self.transaction() as c:
                c.execute(sql, (key, value, now_iso()))

    # ---- discovery ----------------------------------------------------
    def add_discovered(self, items: list[Any], category_slug: str, next_cursor: tuple[str, str] | None = None) -> int:
        """Insert newly seen listings (atomically with the pagination cursor). Returns #new."""
        new = 0
        with self.transaction() as c:
            for it in items:
                cur = c.execute(
                    "INSERT OR IGNORE INTO discovered_urls(url, external_id, status, first_seen_at, "
                    "category_slug, summary_json) VALUES(?,?,?,?,?,?)",
                    (it.url, it.external_id, DISCOVERED, now_iso(), category_slug,
                     orjson.dumps(it.summary).decode()))
                new += cur.rowcount
            if next_cursor:
                self.set_state(next_cursor[0], next_cursor[1], c)
        return new

    def recover_interrupted(self) -> int:
        """Rows left PROCESSING by a crash/kill go back to the queue."""
        with self.transaction() as c:
            return c.execute("UPDATE discovered_urls SET status='DISCOVERED' WHERE status='PROCESSING'").rowcount

    def pending(self, limit: int) -> list[sqlite3.Row]:
        return self.conn.execute(
            "SELECT * FROM discovered_urls WHERE status='DISCOVERED' ORDER BY first_seen_at, rowid LIMIT ?",
            (limit,)).fetchall()

    # ---- per-URL state transitions --------------------------------------
    def mark_processing(self, url: str) -> None:
        with self.transaction() as c:
            c.execute("UPDATE discovered_urls SET status='PROCESSING', attempt_count=attempt_count+1, "
                      "last_attempt_at=? WHERE url=?", (now_iso(), url))

    def release(self, url: str, error: str | None = None, http_status: int | None = None,
                count_attempt: bool = False) -> None:
        """Back to the queue (blocked, interrupted, or retryable error)."""
        with self.transaction() as c:
            c.execute("UPDATE discovered_urls SET status='DISCOVERED', error=?, http_status=?, "
                      "attempt_count=MAX(0, attempt_count-?) WHERE url=?",
                      (error, http_status, 0 if count_attempt else 1, url))

    def mark_failed(self, url: str, error: str, http_status: int | None = None) -> None:
        with self.transaction() as c:
            row = c.execute("SELECT external_id, attempt_count FROM discovered_urls WHERE url=?", (url,)).fetchone()
            c.execute("UPDATE discovered_urls SET status='FAILED', error=?, http_status=? WHERE url=?",
                      (error, http_status, url))
            c.execute("INSERT INTO failed_urls(url, external_id, attempt_count, http_status, error, failed_at) "
                      "VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET attempt_count=excluded.attempt_count, "
                      "http_status=excluded.http_status, error=excluded.error, failed_at=excluded.failed_at",
                      (url, row["external_id"] if row else None, row["attempt_count"] if row else 0,
                       http_status, error, now_iso()))
            c.execute("DELETE FROM processed_urls WHERE url=?", (url,))

    def attempts(self, url: str) -> int:
        row = self.conn.execute("SELECT attempt_count FROM discovered_urls WHERE url=?", (url,)).fetchone()
        return row["attempt_count"] if row else 0

    def save_listing(self, listing: Listing, http_status: int | None = 200) -> str:
        """Dedup + persist + mark SUCCESS/SKIPPED in ONE transaction.

        Priority: external_id, then normalized URL, then content hash.
        Returns: 'inserted' | 'updated' | 'unchanged' | 'duplicate'.
        """
        data = orjson.dumps(listing.model_dump(mode="json")).decode()
        with self.transaction() as c:
            existing = c.execute("SELECT external_id, content_hash FROM listings WHERE external_id=? OR url=?",
                                 (listing.external_id, listing.url)).fetchone()
            if existing:
                same = existing["content_hash"] == listing.content_hash and listing.content_hash is not None
                outcome = "unchanged" if same else "updated"
                if not same:
                    c.execute("UPDATE listings SET url=?, content_hash=?, data_json=?, scraped_at=? WHERE external_id=?",
                              (listing.url, listing.content_hash, data, listing.scraped_at, existing["external_id"]))
                status, error = SUCCESS, None
            else:
                dup = None
                if listing.content_hash:
                    dup = c.execute("SELECT external_id FROM listings WHERE content_hash=? LIMIT 1",
                                    (listing.content_hash,)).fetchone()
                if dup:
                    outcome, status, error = "duplicate", SKIPPED, f"duplicate_of:{dup['external_id']}"
                else:
                    c.execute("INSERT INTO listings(external_id,url,content_hash,data_json,scraped_at) VALUES(?,?,?,?,?)",
                              (listing.external_id, listing.url, listing.content_hash, data, listing.scraped_at))
                    outcome, status, error = "inserted", SUCCESS, None
            now = now_iso()
            c.execute("UPDATE discovered_urls SET status=?, error=?, http_status=?, content_hash=?, "
                      "last_success_at=? WHERE external_id=?",
                      (status, error, http_status, listing.content_hash, now, listing.external_id))
            c.execute("INSERT INTO processed_urls(url, external_id, status, content_hash, http_status, processed_at) "
                      "VALUES(?,?,?,?,?,?) ON CONFLICT(url) DO UPDATE SET status=excluded.status, "
                      "content_hash=excluded.content_hash, http_status=excluded.http_status, "
                      "processed_at=excluded.processed_at",
                      (listing.url, listing.external_id, status, listing.content_hash, http_status, now))
            c.execute("DELETE FROM failed_urls WHERE url=?", (listing.url,))
        return outcome

    # ---- reporting ------------------------------------------------------
    def counts(self, full: bool = True) -> dict[str, int]:
        """Status counts. `full=False` skips the (slower) listings/duplicates scans."""
        rows = self.conn.execute("SELECT status, COUNT(*) n FROM discovered_urls GROUP BY status").fetchall()
        by = {r["status"]: r["n"] for r in rows}
        discovered = sum(by.values())
        out = {
            "discovered": discovered,
            "success": by.get(SUCCESS, 0),
            "failed": by.get(FAILED, 0),
            "skipped": by.get(SKIPPED, 0),
            "pending": by.get(DISCOVERED, 0) + by.get(PROCESSING, 0),
        }
        out["processed"] = out["success"] + out["failed"] + out["skipped"]
        if not full:
            return out
        out["listings"] = self.conn.execute("SELECT COUNT(*) FROM listings").fetchone()[0]
        out["duplicates"] = self.conn.execute(
            "SELECT COUNT(*) FROM discovered_urls WHERE error LIKE 'duplicate_of:%'").fetchone()[0]
        return out

    def failed_rows(self, limit: int = 50) -> list[sqlite3.Row]:
        return self.conn.execute("SELECT * FROM failed_urls ORDER BY failed_at DESC LIMIT ?", (limit,)).fetchall()

    def requeue_failed(self) -> int:
        with self.transaction() as c:
            n = c.execute("UPDATE discovered_urls SET status='DISCOVERED', attempt_count=0, error=NULL "
                          "WHERE status='FAILED'").rowcount
            c.execute("DELETE FROM failed_urls")
        return n

    def iter_listings(self) -> Iterator[dict[str, Any]]:
        cur = self.conn.execute("SELECT data_json FROM listings ORDER BY CAST(external_id AS INTEGER)")
        for row in cur:
            yield orjson.loads(row["data_json"])

    def reset(self) -> None:
        with self.transaction() as c:
            for table in ("discovered_urls", "processed_urls", "failed_urls", "crawler_state", "listings"):
                c.execute(f"DELETE FROM {table}")
        self.conn.execute("VACUUM")

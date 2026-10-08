"""Per-category pagination cursors, stored in crawler_state so `resume` continues exactly
where the last run stopped."""

from __future__ import annotations

import orjson

from crawler.storage.database import Database


class Checkpoint:
    def __init__(self, db: Database) -> None:
        self.db = db

    @staticmethod
    def cursor_key(slug: str) -> str:
        return f"cursor:{slug}"

    def next_page(self, slug: str) -> int:
        return int(self.db.get_state(self.cursor_key(slug), "1") or 1)

    def is_done(self, slug: str) -> bool:
        return self.db.get_state(f"done:{slug}") == "1"

    def mark_done(self, slug: str) -> None:
        self.db.set_state(f"done:{slug}", "1")

    def reopen(self, slug: str) -> None:
        """Start a finished category over (new listings appear at the top over time)."""
        self.db.set_state(f"done:{slug}", "0")
        self.db.set_state(self.cursor_key(slug), "1")

    def save_categories(self, slugs: list[str]) -> None:
        self.db.set_state("categories", orjson.dumps(slugs).decode())

    def categories(self) -> list[str] | None:
        raw = self.db.get_state("categories")
        return orjson.loads(raw) if raw else None

    def note_run(self, stop_reason: str) -> None:
        self.db.set_state("last_run", orjson.dumps({"stopped": stop_reason}).decode())

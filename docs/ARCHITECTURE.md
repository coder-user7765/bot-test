# Architecture

```
 categories ──► pagination ──► listing URLs ──► SQLite queue ──► workers
 (menu)         (search API)    (discovery)      discovered_urls    │
                                                                    ▼
   JSONL (append)  ◄── SQLite listings ◄── Deduplicator ◄── Normalizer ◄── Parser ◄── HTTP client
   (data/normalized)    (source of truth)                                              (rate limited)
```

| Stage | Module | Responsibility |
|---|---|---|
| Discovery | `discovery/categories.py`, `pagination.py`, `listings.py` | menu → category slugs → one search page → `DiscoveredListing(id, url, summary)`. Knows nothing about parsing |
| Queue | `storage/database.py` (`discovered_urls`) | durable work queue; status `DISCOVERED → PROCESSING → SUCCESS/FAILED/SKIPPED` |
| HTTP client | `http/client.py` | the only module that touches the network: global rate limiter, tenacity retries, 429/403/CAPTCHA handling, robots.txt, request budget, interruptible sleeps |
| Parser | `extraction/*` | raw payload/HTML → `Listing` (no cleaning policy) |
| Normalizer | `normalization/listing_normalizer.py` | text hygiene, price parsing, date → UTC, image de-dup, `content_hash` |
| Deduplicator | `Database.save_listing` | external_id → normalised URL → content hash, inside the same transaction as the state change |
| SQLite | `storage/database.py`, `checkpoint.py` | state + `listings` table (authoritative data) |
| JSONL | `storage/json_writer.py` | append during the crawl; `export` rebuilds everything atomically from SQLite |
| Orchestration | `crawler/coordinator.py`, `workers.py`, `cli.py` | wiring, limits, signals, dashboard |

Dependencies point one way: `cli → coordinator → (discovery, workers) → (http, extraction, normalization, storage) → models/utils`.
Modules do not import each other sideways, so each can be tested alone (see `tests/`).

## Crash safety

* SQLite in WAL mode with `synchronous=FULL`; **every state change is its own committed transaction**.
* A listing's data, its `SUCCESS` status and its `processed_urls` row are written in **one** transaction
  (`save_listing`), the pagination cursor is advanced in the **same** transaction as the discovered URLs.
* Rows left `PROCESSING` by a crash/kill are put back in the queue at the next start (`recover_interrupted`).
* JSONL is appended *after* the DB commit and fsynced. Worst case after a crash: a record is in SQLite but not yet in
  the JSONL → `python -m crawler export` regenerates the JSONL from SQLite.
* Verified: Ctrl+C, second Ctrl+C (forced), `kill -9` mid-request, `PRAGMA integrity_check` → `ok`, resume continues.

## Concurrency & politeness

`max_concurrent_requests` workers (default 2) share **one** rate limiter: request *start times* are spaced by
`request_delay_seconds × slow_factor + random jitter`, so concurrency only overlaps latency; it never raises the rate.
HTTP 429 doubles `slow_factor` (up to 16×), pauses *all* workers for `Retry-After` / exponential backoff, and after
`max_block_events` consecutive blocks the crawl stops. CAPTCHA/Cloudflare markers stop it immediately.

## Pagination semantics

Search results are ordered by last refresh, newest first. Because listings move while you crawl, page *N* can
contain items already seen on page *N-1* (duplicates are ignored by id) and rarely skip an item. Use
`--restart-categories` for a later pass to pick up what was missed.

## Future AI use (not implemented)

The dataset is designed for later work: stable ids and `content_hash` (duplicate detection), clean multilingual
`title`/`description` (embeddings, semantic search, RAG, NLP), numeric `price.amount` + `price.unit` +
location + `attributes` (price prediction, recommendation, attribute extraction), category labels
(classification), image URLs with positions (computer vision), `published_at`/`refreshed_at` (time features).

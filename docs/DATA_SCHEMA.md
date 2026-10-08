# Data schema

Authoritative output: `data/normalized/listings.jsonl` – UTF-8, one JSON object per line, `ensure_ascii=false`
(Arabic/French/English stored as real characters). Unknown values are `null` – nothing is invented.

```json
{
  "source": "ouedkniss",
  "external_id": "54926590",
  "url": "https://www.ouedkniss.com/<slug>-d54926590",
  "title": "…",
  "category": "Informatique",
  "subcategory": "Cartes graphique",
  "price":    { "amount": 285000, "currency": "DZD", "raw": "285000 UNIT", "unit": "UNIT" },
  "location": { "wilaya": "Oran", "commune": "Bir el djir", "raw": "Bir el djir, Oran" },
  "description": "…",
  "published_at": "2026-04-14T11:12:53Z",
  "seller": { "display_name": "…", "type": "store" },
  "attributes": { "etat": "Etat neuf" },
  "images": [ { "url": "https://cdn7.ouedkniss.com/1600/…jpg", "position": 0, "alt": null, "width": null, "height": null, "source": "listing" } ],
  "source_metadata": { "…": "see below" },
  "scraped_at": "2026-10-08T06:00:00Z",
  "content_hash": "<sha256>"
}
```

Differences from the originally requested shape (all additive): `price.unit` (needed to interpret per-m² prices),
and extra keys inside `source_metadata`.

| Field | Type | Notes |
|---|---|---|
| `price.amount` | number \| null | DZD. `null` if no price / price 0 |
| `price.raw` | string \| null | as displayed (`6 MILLION` = 6 million *centimes* = 60,000 DA) |
| `price.unit` | string \| null | `UNIT`, `MILLION`, `BILLION`, `UNIT_PER_SQUARE`, `MILLION_PER_SQUARE` … |
| `attributes` | object | keys = site codenames (`marque`, `etat`, `superficie_batie` …), values = displayed text or list. Differs per category |
| `images[].width/height/alt` | null | the API does not provide them |
| `content_hash` | string \| null | SHA-256 of title, description, category, subcategory, price, location, seller, attributes. **Not** of id/url/time/images. `null` when there is neither title nor description |

`source_metadata` (only non-empty keys): `slug`, `reference`, `refreshed_at`, `status`, `price_type`
(`FIXED`/`NEGOTIABLE`/…), `price_preview`, `old_price`, `exchange_type`, `has_delivery`, `delivery_type`,
`quantity`, `street_name`, `has_phone`/`has_email` (booleans only – **no contact data is collected**),
`category_id`, `category_slug`, `category_path`, `category_ids`, `cities` (when several), `store`
(id, slug, description, followers, announcements_count, addresses, …), `attribute_labels`, `variants`,
`is_detail_record`.

## Other files

| File | Content |
|---|---|
| `data/normalized/listings.json` | same records as one JSON array (convenient, not for huge sets) |
| `data/normalized/images.jsonl` | one image per line: `external_id, listing_url, url, position, alt, width, height, source` |
| `data/exports/summary.json` | `total_listings, with_price, with_images, with_description, with_location, duplicates, failed` |
| `data/exports/data-quality.json` | summary + `with_title, with_seller, with_category, with_published_at, pending, discovered` |
| `data/raw/<id>.json` | only if `save_raw_html: true`: raw detail payload (pages are JS-rendered, so JSON, not HTML) |
| `data/images/<id>/NN.jpg` | only if `download_images: true` |

## SQLite (`data/crawler.db`)

| Table | Purpose |
|---|---|
| `discovered_urls` | queue + per-URL tracking: `url, external_id, status, attempt_count, first_seen_at, last_attempt_at, last_success_at, http_status, error, content_hash, category_slug, summary_json` |
| `processed_urls` | URLs that reached `SUCCESS`/`SKIPPED` (with hash, time) |
| `failed_urls` | URLs that failed permanently (error, HTTP status, attempts) |
| `crawler_state` | pagination cursors (`cursor:<category>`), `done:<category>`, category list, last run |
| `listings` | the normalised listings (one row per `external_id`, JSON in `data_json`) |

Statuses: `DISCOVERED` (queued) · `PROCESSING` (in flight; re-queued after a crash) · `SUCCESS` · `FAILED` · `SKIPPED` (content duplicate of another listing: `error = duplicate_of:<id>`).

## Deduplication

1. `external_id` – same id ⇒ same record (re-processing yields `unchanged` or `updated`).
2. normalised URL (lower-case host, no fragment/tracking params, sorted query, no trailing slash).
3. `content_hash` – different id, identical content ⇒ stored once, the later one is `SKIPPED` and counted in `duplicates`.

The title alone is never used. Note that shops re-posting identical text/price/location under new ids are therefore
stored once on purpose; if you would rather keep them, remove the content-hash branch in `Database.save_listing`.

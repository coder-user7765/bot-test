# Parsing strategy (what was actually found on the live site)

Inspected on 2026-10-08 with the crawler's own honest User-Agent, a handful of requests, 2+ s apart.

## 1. Finding: the public HTML contains no listings

| URL | Result |
|---|---|
| `/robots.txt` | `User-agent: *` / `Allow: /` + a sitemap URL that returns **404** |
| `/`, `/informatique/1`, `/automobiles_vehicules/1`, any `/…-d<id>` | HTTP 200, **identical 7 KB shell**: `<div id="app"></div>` + script tags. No JSON-LD, no embedded JSON, no listing markup |
| `/sitemap.xml`, `/sitemap/sitemap-index.xml` | 404 |

So CSS selectors / JSON-LD **cannot** work against today's site: nothing is server-rendered. The site's
own JavaScript front-end (Vue) loads everything from `https://api.ouedkniss.com/graphql`
(`<link rel="preconnect" href="https://api.ouedkniss.com/graphql">` in the shell). The GraphQL documents
the front-end uses are shipped in its public JS bundles; `src/crawler/http/queries.py` contains trimmed
copies. `api.ouedkniss.com/robots.txt` returns 404 (= no restrictions, RFC 9309) and the crawler still
checks it on every run.

> **Terms of use:** this endpoint is not a documented public API. It is what any visitor's browser calls,
> but you remain responsible for checking Ouedkniss' terms. Keep the defaults conservative.

## 2. URL structure

| Thing | Pattern |
|---|---|
| Category (listing page) | `/<category_slug>/<page>` e.g. `/informatique/1` (slug of a sub-category is `informatique-pieces-pc-fixe-carte-graphique`) |
| Listing | `/<slug>-d<id>` e.g. `/…-oran-algerie-d54926590` → `external_id = 54926590` |
| Pagination | page number; search API `filter.page` (1-based) + `filter.count` (page size, 48 works; the site uses 12). `paginatorInfo { currentPage lastPage hasMorePages total }`. Pages far beyond the end return an empty list |
| Top-level categories | `menuFetch` (`listingMenu`) → `target { ... on Category { id name slug } }` – 16 categories + link entries such as "Boutiques" (no slug, skipped) |

## 3. Extraction pipeline (`extraction/listing_parser.py`)

1. **Discovery** – `search(filter:{categorySlug,page,count,orderByField:{field:REFRESHED_AT}})` → cheap summary per listing (id, slug, title, price, cities, 1 image …).
2. **Detail** – `announcementDetails(id)` → everything a visitor sees on the listing page: description, all `medias`, `specs`, category tree, seller/store, cities.
3. **Mapping** – `parse_api_announcement()` maps both shapes to `Listing`.

| Listing field | Source (GraphQL field) | Notes |
|---|---|---|
| `external_id` | `id` | |
| `url` | `slug` + `id` → `/{slug}-d{id}` | normalised (no tracking params, no trailing slash) |
| `title`, `description` | `title`, `description` | whitespace/NBSP/zero-width cleaned, newlines kept in description |
| `category` / `subcategory` | `category.parentTree[0].name` / `category.name` | full path in `source_metadata.category_path`; top-level listing → `subcategory = null` |
| `price.amount` | `price` | **DZD**. `0`/null → `null` |
| `price.raw` | `"{pricePreview} {priceUnit}"` e.g. `6 MILLION`, `65000 UNIT` | what the site displays; `null` when no price |
| `price.unit` | `priceUnit` (`UNIT`, `MILLION`, `BILLION`, `*_PER_SQUARE`) | `*_PER_SQUARE` = price per m² |
| `price.currency` | `"DZD"` when an amount exists | the site prices in DA only (assumption; documented) |
| `location.commune` / `wilaya` | `cities[0].name` / `cities[0].region.name` | never guessed; extra cities in `source_metadata.cities` |
| `location.raw` | `street_name, commune, wilaya` | |
| `published_at` | `createdAt` (UTC) | the front-end aliases `refreshedAt` to "createdAt"; we keep the real `createdAt` and put `refreshedAt` in `source_metadata.refreshed_at` |
| `seller.display_name` / `type` | `store.name` (`store`) if `isFromStore`, else `user.displayName` (`individual`) | public display names only. `username`, avatar, messenger link, phone, e-mail are **not requested** |
| `attributes` | `specs[].specification.codename` → `valueText` (fallback `value`) | single-element lists collapsed; empty specs dropped; labels in `source_metadata.attribute_labels` |
| `images` | `medias(size: LARGE)[].mediaUrl` (fallback `defaultMedia`) | non-image MIME types skipped, de-duplicated, positions 0-based, URLs normalised |

### Price units – important

`price` is the amount in **DA**. `pricePreview`/`priceUnit` is the *display* form. Ouedkniss follows the
Algerian convention where "1 million" = 1,000,000 **centimes** = **10,000 DA** (e.g. `6 MILLION` ⇒ `price = 60000`;
`1.7 BILLION` ⇒ `17,000,000`). The crawler trusts the API's `price`; the text parser (`parse_price_text`) uses the
same convention for strings like `1,5 Millions`.

## 4. Secondary (HTML) strategies

Kept for the case the site ever server-renders listings, and for the fixtures. In priority order
(`parse_html_listing`): embedded JSON → JSON-LD (schema.org `Product`/`Offer`) → semantic HTML
(`h1`, `og:*`, `canonical`, `time[datetime]`, `itemprop`) → CSS selectors (`SELECTORS` dict at the top of
`listing_parser.py`). **These selectors are generic placeholders, not verified against the live site** because
the live HTML has nothing to select. If a field is unavailable the result is `null`; ids are never invented.

## 5. When the site changes

Symptoms: many `FAILED` rows with `GraphQL error`/`BAD_USER_INPUT`. Fix: re-run the discovery steps above
(download the JS bundles referenced from the shell, search for `kind:"OperationDefinition"`), update
`http/queries.py`, run `pytest`, then `python -m crawler failed --requeue`.

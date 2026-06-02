# Review Scraper

A command-line Python tool that collects **retrievable public app reviews** from
**Google Play Store** and **Apple App Store** URLs, normalizes them into one
shared schema, deduplicates them, and exports/stores them. An optional, separate
AI grouping step can classify the collected reviews by sentiment, topic, intent,
and severity.

> **Scope note.** Public scraping can only collect *retrievable* public reviews.
> It **cannot** guarantee every historical review ever written. Public access is
> limited by country, language, store behavior, pagination, and undocumented API
> limits. For an **owned** app, the official Google Play / App Store Connect APIs
> are the better source. Public scrapers like this one are intended for
> **competitor** apps, where official API access is not available.

---

## Features

- Accepts a Google Play **or** Apple App Store URL and auto-detects the platform.
- Extracts the correct app identifier:
  - Google Play: package name from the `id` query parameter (e.g. `com.company.app`).
  - Apple App Store: numeric app id from the URL path (e.g. `id123456789`), and
    the storefront country from the path (e.g. `/us/app/...`) when present.
- Configurable countries and languages.
- Normalizes both stores into one common schema.
- Deterministic deduplication across repeated runs (idempotent).
- Exports to **CSV** or **JSON**, and/or stores in **SQLite**.
- Per-country failure isolation: one country failing does not abort the run, and
  partial results are still saved.
- Optional, separate **AI grouping** command (does not re-scrape).

## Requirements

- **Python 3.9+**
- Dependencies in [requirements.txt](requirements.txt):
  - [`google-play-scraper`](https://pypi.org/project/google-play-scraper/)
  - [`app-store-web-scraper`](https://pypi.org/project/app-store-web-scraper/)
  - `pytest` (only for running the tests)

This MVP does **not** use a generic browser scraper, Playwright, or Selenium.

## Installation

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
```

## Usage

Run the CLI from inside the `review_scraper` directory:

```bash
cd review_scraper
```

### Scrape Google Play (export to CSV)

```bash
python main.py scrape "https://play.google.com/store/apps/details?id=com.example.app" \
    --countries us gb pt --lang en --out reviews.csv
```

### Scrape Apple App Store (export to JSON)

```bash
python main.py scrape "https://apps.apple.com/us/app/example-app/id123456789" \
    --countries us gb pt --out reviews.json
```

### Scrape into SQLite storage

```bash
python main.py scrape "https://play.google.com/store/apps/details?id=com.example.app" \
    --countries us --lang en --db reviews.sqlite
```

You can combine `--out` and `--db` to export and store in one run.

### AI grouping (separate post-processing step)

```bash
python main.py group --input reviews.csv --out grouped_reviews.csv
```

`group` reads already-collected reviews (CSV or JSON) and adds grouping fields.
It never scrapes. Use `--backend llm` to plug in a real model (falls back to the
deterministic rule-based classifier when no model is configured).

### Useful scrape options

| Option            | Description                                                        |
| ----------------- | ------------------------------------------------------------------ |
| `--countries`     | Country codes to scrape, e.g. `us gb pt`.                          |
| `--lang`          | Language codes (Google Play only), e.g. `en pt`. Default: `en`.   |
| `--max-reviews`   | Cap reviews per country/language. Default: all retrievable.       |
| `--most-relevant` | Fetch most-relevant first instead of newest first (Google Play).  |
| `--out`           | Export path; `.csv` or `.json`.                                   |
| `--db`            | SQLite database path.                                             |
| `-v` / `--verbose`| Debug logging.                                                    |

## Normalized review schema

Every review — from either store — is normalized to:

| Field                     | Notes                                              |
| ------------------------- | -------------------------------------------------- |
| `platform`                | `google_play` or `app_store`                       |
| `source_url`              | Original URL provided by the user                  |
| `app_id`                  | Google package name or Apple numeric app id        |
| `country`                 | Country code used for scraping                      |
| `language`                | Language code when available (Google Play)         |
| `review_id`               | Stable review id when available                    |
| `rating`                  | Integer rating                                     |
| `title`                   | Review title when available (App Store)            |
| `text`                    | Review body                                        |
| `reviewer_name`           | Public reviewer name/alias when available          |
| `review_date`             | ISO date when available                            |
| `app_version`             | App version when available                         |
| `developer_response`      | Developer response text when available             |
| `developer_response_date` | Developer response date when available             |
| `scraped_at`              | UTC timestamp of collection                         |

### Deduplication rules

- Prefer `platform + app_id + country + review_id` when a `review_id` exists.
- Otherwise generate a SHA-256 hash from
  `platform + app_id + country + rating + title + text + review_date`.
- Repeated runs are idempotent: exported files and the SQLite table never contain
  duplicate rows.

## SQLite storage

- A `reviews` table mirrors the normalized schema.
- A `dedup_key` column plus a **UNIQUE index** enforce deduplication.
- `created_at` / `updated_at` timestamps track inserts and refreshes.
- The storage layer is behind a small abstract `ReviewStore` interface
  ([storage.py](review_scraper/storage.py)), so a Postgres adapter can replace
  the SQLite one later without touching the rest of the pipeline.

## AI grouping output

The `group` command adds these fields to each review:

- `sentiment`: `positive` | `neutral` | `negative`
- `topic`: `bugs` | `pricing` | `onboarding` | `performance` | `login` |
  `payments` | `support` | `feature_request` | `UX` | `other`
- `intent`: `bug_report` | `complaint` | `praise` | `feature_request` |
  `question` | `churn_risk`
- `severity`: `low` | `medium` | `high`
- `summary`: one-sentence summary of the review

The default backend is a deterministic rule-based classifier (no network, no
keys). The `llm` backend is a stub hook for wiring in a real model later.

## Project structure

```
review_scraper/
  main.py            # CLI: `scrape` and `group` subcommands
  url_parser.py      # platform detection + app-id/country extraction
  normalizer.py      # shared schema + dedup key/helpers
  storage.py         # abstract ReviewStore + SQLite implementation
  exporters.py       # CSV/JSON export and load
  stores/
    google_play.py   # google-play-scraper adapter
    app_store.py     # app-store-web-scraper adapter
  ai_grouping/
    classifier.py    # optional post-processing classification
  tests/             # url parsing, normalization, deduplication
requirements.txt
README.md
```

## Running the tests

```bash
cd review_scraper
python -m pytest
```

The tests cover URL parsing, normalization, and deduplication (including SQLite
idempotency) and do **not** require network access.

## Known limitations

- **Not exhaustive.** Only *retrievable* public reviews are collected; stores cap
  pagination and vary by country/language.
- **App Store subset / empty feeds.** Apple exposes reviews through the iTunes
  RSS "customerreviews" feed, which returns at most ~500 reviews per country
  (10 pages) and **often returns an empty feed** — Apple throttles and degrades
  this public endpoint, sometimes returning zero reviews for popular apps across
  all countries. When this happens the run completes cleanly with 0 App Store
  reviews; this is a store-side limitation, not a tool error. Google Play access
  is currently far more reliable.
- **No language facet for App Store.** App Store reviews are collected per
  storefront *country*; `--lang` applies to Google Play only.
- **Rate limits.** Heavy scraping across many countries may be throttled by the
  stores. Failures are logged per country and the run continues with partial
  results.
- **Deterministic by design.** AI is only applied *after* reviews are collected
  and stored, never inside the scraper.

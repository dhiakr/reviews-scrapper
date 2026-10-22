# Review Scraper

Collect **retrievable public reviews** from Google Play and the Apple App Store,
save progress after each page, and export deduplicated results through a Streamlit
web interface or CLI. Public feeds cannot guarantee every historical review.

## Install and run

Requires **Python 3.10+**. From the repository root:

```bash
python -m venv .venv
# Windows: .venv\Scripts\activate
# macOS/Linux: source .venv/bin/activate
pip install -r requirements.txt
streamlit run streamlit_app.py
```

The scraper versions are pinned to `google-play-scraper==1.2.7` and
`app-store-web-scraper==0.2.0`; adapters use their private parsers to expose real
page boundaries and surface failures. Upgrade these dependencies only after
checking the adapter tests and a bounded live request. The frontend uses
`streamlit==1.58.0`.

## Web collection

- Enter the store URL and choose storefront countries. The configured sweep
  contains **109 countries**; it is not a guarantee of every available storefront.
- For Google Play, enter explicit query languages, such as `en pt es`. Every
  selected language is queried in every selected country. The default is English;
  detecting a review's language afterward does not expand query coverage.
- Set an optional limit per country/language, request delay, retries, and parallel
  queries. The default is two concurrent queries, with a maximum of four.
- Collection runs in background workers. Each completed page and its continuation
  checkpoint are committed together to SQLite. Workers stream review bodies to
  disk; loop guards retain identifiers and cursors in memory. Preparing a full
  download still requires memory proportional to the export.
- Keep the run's private recovery code. After a browser refresh, use **Restore a
  saved run**. Interrupted, failed, partial, or cancelled runs can resume unfinished
  queries; previously finished queries are skipped. Stop requests take effect after
  the current request.
- The interface previews up to 200 saved reviews. CSV, JSON, and XLSX downloads
  contain all saved rows included when the download is prepared. Prepare the
  download again to include pages collected afterward.
- Download the coverage report to inspect countries/languages queried, page counts,
  statuses, errors, stop reasons, and the collected date range. A `finished` run means
  its queries stopped normally or hit their limits; it does not establish complete
  historical coverage.

## Persistence and hosting

Saved runs use `.review_scraper/jobs.sqlite3` by default. Set
`REVIEW_SCRAPER_DATA_DIR` to change the data directory. The CLI also accepts an
explicit `--job-db` path.

SQLite checkpoints survive browser refreshes and worker interruptions **while that
database remains available**. They do not make local disk permanent. Streamlit
Community Cloud may reset local files during redeployment or instance replacement;
download results and back up the database. For a durable archive and unattended
collection, run on an always-on host with a persistent volume. A sleeping or stopped
host cannot continue scraping.

Review feeds can change between runs, and Google continuation tokens can expire.
Resuming can overlap or skip reviews if the feed has shifted. If a token is rejected,
start a fresh run; earlier saved rows remain available. Scheduled fresh collections
build an archive over time but cannot recover reviews already removed from the feed.

## CLI

Run these examples from the repository root. Replace the example app identifiers.

```bash
python review_scraper/main.py scrape "https://play.google.com/store/apps/details?id=com.example.app" --countries us gb pt --lang en pt es --workers 2 --job-db saved-runs.sqlite3 --out reviews.csv --report coverage.json

python review_scraper/main.py scrape "https://apps.apple.com/us/app/example-app/id123456789" --countries us gb pt --job-db saved-runs.sqlite3 --out reviews.xlsx --report apple-coverage.json
```

The CLI prints the saved run's recovery code. Resume with the same app URL and job
database; the saved run's original query selection and limits are reused:

```bash
python review_scraper/main.py scrape "https://play.google.com/store/apps/details?id=com.example.app" --job-db saved-runs.sqlite3 --resume RECOVERY_CODE --out reviews.json --report coverage.json
```

`--db archive.sqlite3` additionally maintains a deduplicated review archive across
separate runs. `--job-db` stores run progress and checkpoints; `--db` stores the
combined archive. Both can be used with `--out` and `--report`.

To collect repeatedly on an always-on host:

```bash
python review_scraper/main.py watch "https://play.google.com/store/apps/details?id=com.example.app" --countries us pt --lang en pt --workers 2 --db archive.sqlite3 --job-db saved-runs.sqlite3 --interval 3600 --cycles 24 --report latest-coverage.json
```

`watch` starts a fresh collection each cycle and upserts pages into the archive.
`--interval` is the delay in seconds **after each completed run**; omit `--cycles`
to continue until stopped. The report path is overwritten with the latest cycle's
coverage. Run this process under your host's service manager for unattended use.

| Option | Meaning |
| --- | --- |
| `--countries us gb pt` | Storefront country codes; `all` uses the configured 109-country list. |
| `--lang en pt` | Explicit Google Play query languages; default `en`. Ignored for Apple. |
| `--max-reviews 1000` | Requested maximum per query; omitted means all retrievable. Store limits still apply. |
| `--workers 2` | Concurrent country/language queries, from 1 to 4. |
| `--page-delay-ms 600` | Delay between pages within a query. |
| `--retries 5` | Retry budget for transient request failures. |
| `--most-relevant` | Google Play relevance order instead of newest first. |
| `--out PATH` | Export CSV, JSON, or XLSX. |
| `--report PATH` | Save the run's JSON coverage report. |

## Data and deduplication

The shared schema includes app/platform identifiers, source URL, rating, title,
review text, public reviewer name, review date, app version, developer response and
response date, and collection timestamp. Geography and language are separate:

| Field | Meaning |
| --- | --- |
| `country` | Storefront queried; not a verified reviewer location. |
| `query_language` | Google Play language requested; `null` for Apple. |
| `language` | Best-effort language detected from the review text. |
| `reviewer_country` | Unknown unless the source supplies verified country information. |

Detected language is never used to invent a reviewer country. Short or ambiguous
text may be mislabeled or have no detected language.

When a stable review ID exists, deduplication uses `platform + app_id + review_id`
across countries, languages, and repeated runs. Without an ID, a fallback hash uses
the storefront and review content/date. Repeated sightings update the saved review;
the archive stores a merged current snapshot, not a full edit history. Existing
SQLite review databases migrate to the new schema and deduplication keys when opened.

## Collection limits

- **Apple:** up to **500 recent public reviews per storefront**, across 10 pages.
  The 109-country configured sweep therefore has a theoretical ceiling of 54,500
  rows before deduplication, often fewer. Increasing `--max-reviews` cannot bypass
  this feed limit. Apple has no query-language selection here.
- **Google Play:** 199 reviews per request batch, with no fixed total limit in this
  code. Collection follows continuation tokens. Language, country, store behavior,
  and token availability determine what remains accessible. Strict requests surface
  network, HTTP, and parsing errors rather than silently treating them as empty
  results; transient failures use bounded retries and request timeouts.
- **Both:** later failures retain earlier committed pages and record a partial or
  failed outcome. Empty feeds and exhausted pagination do not prove lifetime
  completeness. Country/language sweeps can overlap; unique totals may be lower
  than the sum of query counts. Ratings without written reviews are not collected.
- For apps you own, prefer official store export/API integrations for historical
  access. Those integrations are not implemented in this public scraper.

## Optional grouping and tests

Grouping reads existing exports and does not scrape:

```bash
python review_scraper/main.py group --input reviews.csv --out grouped_reviews.csv
python -m pytest review_scraper/tests -q
```

The default grouping backend uses deterministic rules for sentiment, topic, intent,
severity, and a short summary. The `llm` option is a stub for a future integration.
Tests cover normalization, deduplication, page persistence, failure handling,
checkpoint recovery, and job execution without requiring live store access.

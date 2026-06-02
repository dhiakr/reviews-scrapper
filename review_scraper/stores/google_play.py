"""Google Play store adapter.

Wraps the ``google-play-scraper`` package and returns normalized review dicts.

Scraping is deterministic: same app/country/language/sort yields the same
ordering from the store's public endpoints. We can only collect *retrievable*
public reviews — the store limits how far pagination goes per country/language.
"""

from __future__ import annotations

import logging
import time
from typing import Any, Dict, List, Optional

from normalizer import detect_language, infer_country, make_review, utc_now_iso
from url_parser import PLATFORM_GOOGLE_PLAY

logger = logging.getLogger(__name__)


def _raw_to_normalized(
    raw: Dict[str, Any],
    *,
    source_url: str,
    app_id: str,
    country: str,
    language: str,
    scraped_at: str,
) -> Dict[str, Any]:
    text = raw.get("content")
    # Detect the review's actual language from its text; the storefront `lang`
    # we queried does not reflect what language the user wrote in. Infer the
    # country from that language since Play review data omits the reviewer's
    # country. Both fall back to the queried values when detection is uncertain.
    detected_lang = detect_language(text)
    review_language = detected_lang or language
    review_country = infer_country(detected_lang) or country
    return make_review(
        platform=PLATFORM_GOOGLE_PLAY,
        source_url=source_url,
        app_id=app_id,
        country=review_country,
        language=review_language,
        review_id=raw.get("reviewId"),
        rating=raw.get("score"),
        title=None,  # Google Play reviews have no separate title.
        text=text,
        reviewer_name=raw.get("userName"),
        review_date=raw.get("at"),
        app_version=raw.get("reviewCreatedVersion") or raw.get("appVersion"),
        developer_response=raw.get("replyContent"),
        developer_response_date=raw.get("repliedAt"),
        scraped_at=scraped_at,
    )


# Number of reviews requested per page. Google caps this around 199.
_PAGE_SIZE = 199


def _fetch_page(reviews_fn, app_id, *, country, language, sort, count, token, retries):
    """Fetch one page, retrying transient failures with exponential backoff."""
    attempt = 0
    while True:
        try:
            return reviews_fn(
                app_id,
                lang=language,
                country=country,
                sort=sort,
                count=count,
                continuation_token=token,
            )
        except Exception as exc:  # noqa: BLE001 - retry transient network errors
            if attempt >= retries:
                raise
            attempt += 1
            backoff = min(2 ** attempt * 0.5, 8.0)
            logger.warning(
                "Google Play page fetch failed (attempt %d/%d): %s — retrying in %.1fs",
                attempt,
                retries,
                exc,
                backoff,
            )
            time.sleep(backoff)


def scrape(
    *,
    app_id: str,
    source_url: str,
    country: str,
    language: str = "en",
    max_reviews: Optional[int] = None,
    newest_first: bool = True,
    page_delay_ms: int = 200,
    max_retries: int = 4,
) -> List[Dict[str, Any]]:
    """Fetch retrievable public reviews for one app/country/language.

    Pages manually through the public endpoint instead of ``reviews_all`` so we
    can (a) pause ``page_delay_ms`` between pages and (b) retry. Crucially, when
    Google ends pagination *after a full page* — the signature of throttling from
    cloud/datacenter IPs — we re-request the same continuation token a few times
    with backoff to squeeze past the cutoff. This recovers many more reviews when
    the scraper runs on a throttled host, while still stopping cleanly on a
    genuine end (a short final page).

    Returns normalized review dicts. Raises on hard scraper failures so the
    caller can record the failure per country and continue.
    """
    # Imported lazily so the package is only required when actually scraping
    # Google Play (keeps `group`-only usage dependency-light).
    from google_play_scraper import Sort, reviews

    sort = Sort.NEWEST if newest_first else Sort.MOST_RELEVANT
    scraped_at = utc_now_iso()
    delay = max(page_delay_ms, 0) / 1000.0

    raw_reviews: List[Dict[str, Any]] = []
    token = None
    while True:
        if max_reviews is not None:
            remaining = max_reviews - len(raw_reviews)
            if remaining <= 0:
                break
            count = min(_PAGE_SIZE, remaining)
        else:
            count = _PAGE_SIZE

        batch, new_token = _fetch_page(
            reviews,
            app_id,
            country=country,
            language=language,
            sort=sort,
            count=count,
            token=token,
            retries=max_retries,
        )
        raw_reviews.extend(batch)
        next_token = getattr(new_token, "token", None)

        # A full page followed by a None token is suspicious: likely a throttled
        # cutoff rather than the true end. Re-request the SAME token (same page
        # content) with backoff to see if Google hands us a real next token.
        if next_token is None and len(batch) >= count and max_retries > 0:
            for attempt in range(1, max_retries + 1):
                time.sleep(min(2 ** attempt * 0.5, 8.0))
                _, recovered = _fetch_page(
                    reviews,
                    app_id,
                    country=country,
                    language=language,
                    sort=sort,
                    count=count,
                    token=token,
                    retries=0,
                )
                if getattr(recovered, "token", None) is not None:
                    new_token = recovered
                    next_token = recovered.token
                    logger.info(
                        "Recovered pagination after a throttled cutoff "
                        "(attempt %d) at %d reviews.",
                        attempt,
                        len(raw_reviews),
                    )
                    break

        if next_token is None:
            break
        token = new_token
        if delay:
            time.sleep(delay)

    # Google returns a full page even when we request fewer, so trim to the cap.
    if max_reviews is not None and len(raw_reviews) > max_reviews:
        raw_reviews = raw_reviews[:max_reviews]

    logger.info(
        "Google Play: fetched %d raw reviews for %s [country=%s lang=%s]",
        len(raw_reviews),
        app_id,
        country,
        language,
    )

    return [
        _raw_to_normalized(
            raw,
            source_url=source_url,
            app_id=app_id,
            country=country,
            language=language,
            scraped_at=scraped_at,
        )
        for raw in raw_reviews
    ]

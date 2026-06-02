"""Google Play store adapter.

Wraps the ``google-play-scraper`` package and returns normalized review dicts.

Scraping is deterministic: same app/country/language/sort yields the same
ordering from the store's public endpoints. We can only collect *retrievable*
public reviews — the store limits how far pagination goes per country/language.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from normalizer import make_review, utc_now_iso
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
    return make_review(
        platform=PLATFORM_GOOGLE_PLAY,
        source_url=source_url,
        app_id=app_id,
        country=country,
        language=language,
        review_id=raw.get("reviewId"),
        rating=raw.get("score"),
        title=None,  # Google Play reviews have no separate title.
        text=raw.get("content"),
        reviewer_name=raw.get("userName"),
        review_date=raw.get("at"),
        app_version=raw.get("reviewCreatedVersion") or raw.get("appVersion"),
        developer_response=raw.get("replyContent"),
        developer_response_date=raw.get("repliedAt"),
        scraped_at=scraped_at,
    )


def scrape(
    *,
    app_id: str,
    source_url: str,
    country: str,
    language: str = "en",
    max_reviews: Optional[int] = None,
    newest_first: bool = True,
) -> List[Dict[str, Any]]:
    """Fetch retrievable public reviews for one app/country/language.

    Returns normalized review dicts. Raises on hard scraper failures so the
    caller can record the failure per country and continue.
    """
    # Imported lazily so the package is only required when actually scraping
    # Google Play (keeps `group`-only usage dependency-light).
    from google_play_scraper import Sort, reviews, reviews_all

    sort = Sort.NEWEST if newest_first else Sort.MOST_RELEVANT
    scraped_at = utc_now_iso()

    if max_reviews is None:
        # Page through everything the public endpoint will return.
        raw_reviews = reviews_all(
            app_id,
            lang=language,
            country=country,
            sort=sort,
            sleep_milliseconds=100,
        )
    else:
        raw_reviews, _ = reviews(
            app_id,
            lang=language,
            country=country,
            sort=sort,
            count=max_reviews,
        )

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

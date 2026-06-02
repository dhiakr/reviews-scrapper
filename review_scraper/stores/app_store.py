"""Apple App Store store adapter.

Wraps the ``app-store-web-scraper`` package and returns normalized review dicts.

The public App Store RSS/web sources only expose a *subset* of reviews per
storefront (capped pagination, per-country differences). We collect what is
retrievable and never claim completeness.
"""

from __future__ import annotations

import logging
from typing import Any, Dict, List, Optional

from normalizer import detect_language, make_review, utc_now_iso
from url_parser import PLATFORM_APP_STORE

logger = logging.getLogger(__name__)

# App Store storefront country codes (ISO 3166-1 alpha-2). Apple's public RSS
# feed caps reviews at ~500 per storefront, so to collect "as much as they can"
# you sweep every storefront and deduplicate. Use this with `--countries all`
# (CLI) or the "all storefronts" option in the UI.
ALL_COUNTRIES = [
    "us", "gb", "ca", "au", "nz", "ie", "za", "in", "sg", "my", "ph", "hk",
    "id", "th", "vn", "tw", "jp", "kr", "cn",
    "de", "fr", "es", "it", "pt", "nl", "be", "lu", "at", "ch", "se", "no",
    "dk", "fi", "is", "pl", "cz", "sk", "hu", "ro", "bg", "hr", "si", "rs",
    "gr", "tr", "ua", "ee", "lv", "lt", "by", "md", "mt", "cy",
    "br", "mx", "ar", "cl", "co", "pe", "ve", "ec", "uy", "py", "bo", "cr",
    "gt", "hn", "ni", "pa", "sv", "do", "jm", "tt",
    "ae", "sa", "qa", "kw", "bh", "om", "jo", "lb", "il", "eg", "ma", "dz",
    "tn", "ng", "ke", "gh", "tz", "ug", "ci", "sn", "cm",
    "pk", "bd", "lk", "np", "kz", "uz", "az", "ge", "am", "kh", "la", "mn",
    "mo", "bn", "fj",
]


def _attr(obj: Any, *names: str) -> Optional[Any]:
    """Return the first present, non-None attribute among ``names``.

    The App Store review object's exact attribute names can vary between
    library versions, so we probe defensively.
    """
    for name in names:
        if hasattr(obj, name):
            value = getattr(obj, name)
            if value is not None:
                return value
    return None


def _review_to_normalized(
    review: Any,
    *,
    source_url: str,
    app_id: str,
    country: str,
    scraped_at: str,
) -> Dict[str, Any]:
    title = _attr(review, "title")
    text = _attr(review, "content", "review", "text", "body")
    # Detect the language the review was written in. The storefront country is
    # kept as-is (it is the real country where the review was posted).
    language = detect_language(text) or detect_language(title)
    return make_review(
        platform=PLATFORM_APP_STORE,
        source_url=source_url,
        app_id=app_id,
        country=country,
        language=language,
        review_id=_attr(review, "id", "review_id"),
        rating=_attr(review, "rating", "score"),
        title=title,
        text=text,
        reviewer_name=_attr(review, "user_name", "userName", "author", "nickname"),
        review_date=_attr(review, "date", "updated", "review_date"),
        app_version=_attr(review, "app_version", "version"),
        developer_response=_attr(review, "developer_response", "reply", "response"),
        developer_response_date=_attr(
            review, "developer_response_date", "reply_date"
        ),
        scraped_at=scraped_at,
    )


def scrape(
    *,
    app_id: str,
    source_url: str,
    country: str,
    max_reviews: Optional[int] = None,
) -> List[Dict[str, Any]]:
    """Fetch retrievable public reviews for one app in one storefront/country.

    Returns normalized review dicts. Raises on hard scraper failures so the
    caller can record the failure per country and continue.
    """
    # Imported lazily so the package is only required when actually scraping
    # the App Store.
    from app_store_web_scraper import AppStoreEntry

    scraped_at = utc_now_iso()
    entry = AppStoreEntry(app_id=int(app_id), country=country)

    collected: List[Any] = []
    # ``reviews()`` returns an iterator/iterable; ``limit`` caps retrieval.
    review_iter = entry.reviews(limit=max_reviews) if max_reviews else entry.reviews()
    for review in review_iter:
        collected.append(review)
        if max_reviews is not None and len(collected) >= max_reviews:
            break

    logger.info(
        "App Store: fetched %d raw reviews for id%s [country=%s]",
        len(collected),
        app_id,
        country,
    )

    return [
        _review_to_normalized(
            review,
            source_url=source_url,
            app_id=app_id,
            country=country,
            scraped_at=scraped_at,
        )
        for review in collected
    ]

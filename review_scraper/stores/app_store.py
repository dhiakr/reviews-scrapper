"""Apple App Store store adapter.

Wraps the ``app-store-web-scraper`` package and returns normalized review dicts.

The public App Store RSS/web sources only expose a *subset* of reviews per
storefront (capped pagination, per-country differences). We collect what is
retrievable and never claim completeness.
"""

from __future__ import annotations

import logging
from typing import Any, Callable, Dict, List, Optional

from normalizer import detect_language, make_review, utc_now_iso
from scrape_report import PageCallback, ScrapeResult
from url_parser import PLATFORM_APP_STORE

logger = logging.getLogger(__name__)

# App Store storefront country codes (ISO 3166-1 alpha-2). Apple's public RSS
# feed caps reviews at 500 per storefront. This is a configured list, not a
# guarantee that every current Apple storefront is covered. The historical
# ALL_COUNTRIES name is retained for CLI compatibility.
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
    # The country describes the queried storefront, not verified reviewer
    # geography. The detected language is independent of that storefront.
    language = detect_language(text) or detect_language(title)
    response = _attr(review, "developer_response", "reply", "response")
    response_date = _attr(review, "developer_response_date", "reply_date")
    if response is not None and not isinstance(response, str):
        # Some upstream review objects carry an AppReviewResponse dataclass;
        # export its contents rather than leaking an object into JSON/SQLite.
        response_date = response_date or _attr(response, "modified", "date", "updated")
        response = _attr(response, "body", "text", "content")
    if response is not None and not isinstance(response, str):
        response = str(response)
    return make_review(
        platform=PLATFORM_APP_STORE,
        source_url=source_url,
        app_id=app_id,
        country=country,
        language=language,
        query_language=None,
        reviewer_country=None,
        review_id=_attr(review, "id", "review_id"),
        rating=_attr(review, "rating", "score"),
        title=title,
        text=text,
        reviewer_name=_attr(review, "user_name", "userName", "author", "nickname"),
        review_date=_attr(review, "date", "updated", "review_date"),
        app_version=_attr(review, "app_version", "version"),
        developer_response=response,
        developer_response_date=response_date,
        scraped_at=scraped_at,
    )


def _new_entry(
    app_id: str, country: str, page_delay_ms: int, max_retries: int
) -> Any:
    """Build the pinned scraper's session with finite timeouts and retries.

    The library's iterator does not expose page boundaries or checkpoints.
    Its page parser and session are used directly below; dependency pinning
    makes those private interfaces deliberate and testable.
    """
    from app_store_web_scraper import AppStoreEntry, AppStoreSession
    from urllib3 import Timeout

    numeric_app_id = int(app_id)
    normalized_country = country.lower()
    session = AppStoreSession(
        delay=page_delay_ms / 1000,
        retries=max_retries,
        retries_backoff_factor=0.5,
        retries_backoff_max=10,
    )
    try:
        session._http.connection_pool_kw["timeout"] = Timeout(connect=10, read=30)
        retries = session._http.connection_pool_kw["retries"]
        session._http.connection_pool_kw["retries"] = retries.new(
            status_forcelist={429, 500, 502, 503, 504}
        )
        return AppStoreEntry(app_id=numeric_app_id, country=normalized_country, session=session)
    except Exception:
        session._http.clear()
        raise


def scrape_with_report(
    *,
    app_id: str,
    source_url: str,
    country: str,
    max_reviews: Optional[int] = None,
    page_delay_ms: int = 600,
    max_retries: int = 5,
    resume: Optional[Dict[str, Any]] = None,
    on_page: Optional[PageCallback] = None,
    should_stop: Optional[Callable[[], bool]] = None,
    retain_reviews: bool = True,
) -> ScrapeResult:
    """Fetch public feed pages, preserving progress if a later page fails.

    ``reviews`` contains only this invocation's new rows. ``fetched`` and
    ``pages_fetched`` in the checkpoint include earlier saved invocations.
    A partial page keeps its page number plus an offset so increasing the
    requested limit on resume does not discard the rest of that page.

    Completion of pagination is never proof of complete historical coverage.
    Set ``retain_reviews=False`` with a persistence callback to keep memory
    bounded to the current page; counters and status still include saved rows.
    """
    if max_reviews is not None and max_reviews <= 0:
        raise ValueError("max_reviews must be positive")
    if page_delay_ms < 0 or max_retries < 0:
        raise ValueError("page_delay_ms and max_retries must not be negative")
    if not retain_reviews and on_page is None:
        raise ValueError("on_page is required when retain_reviews is False")

    saved = dict(resume or {})
    page = int(saved.get("next_page", 1))
    fetched = int(saved.get("fetched", 0))
    pages_fetched = int(saved.get("pages_fetched", 0))
    offset = int(saved.get("page_offset", 0))
    if not 1 <= page <= 11 or not 0 <= fetched <= 500 or pages_fetched < 0:
        raise ValueError("Invalid App Store checkpoint")
    if not 0 <= offset < 50:
        raise ValueError("Invalid App Store checkpoint page offset")
    checkpoint = {
        "next_page": page,
        "fetched": fetched,
        "pages_fetched": pages_fetched,
        "page_offset": offset,
    }
    collected: List[Dict[str, Any]] = []
    warnings = [
        "Apple exposes at most 500 recent public reviews per storefront. "
        "Reaching the end of a feed does not establish complete historical coverage."
    ]
    if resume:
        warnings.append(
            "The recent-review feed can change between runs; a resumed page cursor "
            "may overlap or skip reviews inserted or removed since the checkpoint."
        )

    def result(status: str, reason: str, error: Optional[str] = None) -> ScrapeResult:
        return ScrapeResult(
            reviews=collected,
            status=status,
            stop_reason=reason,
            pages_fetched=pages_fetched,
            checkpoint=dict(checkpoint),
            error=error,
            warnings=list(warnings),
        )

    if fetched >= 500 or page > 10:
        return result("limited", "feed_window_limit")
    if max_reviews is not None and fetched >= max_reviews:
        return result("limited", "requested_limit")

    scraped_at = utc_now_iso()
    entry = None
    try:
        entry = _new_entry(app_id, country, page_delay_ms, max_retries)
        while page <= 10 and fetched < 500:
            if should_stop is not None and should_stop():
                return result("cancelled", "cancelled")
            path = (
                f"/{country.lower()}/rss/customerreviews/page={page}/"
                f"id={int(app_id)}/sortby=mostrecent/json"
            )
            data = entry._session._get(path)
            feed = data["feed"]
            if not any(
                link.get("attributes", {}).get("rel") == "self"
                for link in feed["link"]
            ):
                raise ValueError(f"App {app_id} was not found in storefront {country}")
            raw_entries = feed.get("entry", [])
            if not isinstance(raw_entries, list):
                raise ValueError("App Store returned an unexpected reviews feed")
            pages_fetched += 1

            if not raw_entries:
                next_checkpoint = {
                    "next_page": page + 1,
                    "fetched": fetched,
                    "pages_fetched": pages_fetched,
                    "page_offset": 0,
                }
                if on_page is not None:
                    on_page([], dict(next_checkpoint))
                checkpoint = next_checkpoint
                return result("finished", "empty_feed" if fetched == 0 else "endpoint_end")
            if offset > len(raw_entries):
                raise ValueError("Saved page offset no longer exists in the live feed")

            available = raw_entries[offset:]
            remaining = min(500 - fetched, (max_reviews or 500) - fetched)
            selected = available[:remaining]
            normalized = [
                _review_to_normalized(
                    entry._parse_review_entry(raw),
                    source_url=source_url,
                    app_id=app_id,
                    country=country.lower(),
                    scraped_at=scraped_at,
                )
                for raw in selected
            ]
            consumed_page = len(selected) == len(available)
            next_checkpoint = {
                "next_page": page + 1 if consumed_page else page,
                "fetched": fetched + len(normalized),
                "pages_fetched": pages_fetched,
                "page_offset": 0 if consumed_page else offset + len(selected),
            }
            # Persist the rows and cursor together before advancing in memory.
            # Persistence errors surface once; they are never treated as HTTP
            # failures or retried against a different page.
            if on_page is not None:
                on_page(normalized, dict(next_checkpoint))
            if retain_reviews:
                collected.extend(normalized)
            checkpoint = next_checkpoint
            fetched = checkpoint["fetched"]
            page = checkpoint["next_page"]
            offset = checkpoint["page_offset"]
            if fetched >= 500 or page > 10:
                return result("limited", "feed_window_limit")
            if max_reviews is not None and fetched >= max_reviews:
                return result("limited", "requested_limit")
    except Exception as exc:  # retain previously persisted pages on failure
        logger.warning("App Store collection stopped for %s/%s: %s", app_id, country, exc)
        return result("partial" if fetched else "failed", "error", str(exc))
    finally:
        if entry is not None:
            entry._session._http.clear()

    return result("limited", "feed_window_limit")


def scrape(**kwargs: Any) -> List[Dict[str, Any]]:
    """Compatibility wrapper; fail visibly instead of returning silent partial data."""
    outcome = scrape_with_report(**kwargs)
    if outcome.error:
        raise RuntimeError(outcome.error)
    return outcome.reviews

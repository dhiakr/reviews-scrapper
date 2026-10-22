"""Collect retrievable Google Play reviews with explicit pagination outcomes.

The dependency's public reviews() catches HTTP/parsing failures and returns an
empty result. We reuse its pinned format/specifications but make a strict HTTP
request so failure cannot look like the end of the accessible public feed.
"""
from __future__ import annotations

import hashlib
import json
import logging
import ssl
import time
from typing import Any, Callable, Dict, List, Optional
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen

from normalizer import detect_language, make_review, utc_now_iso
from scrape_report import ScrapeResult
from url_parser import PLATFORM_GOOGLE_PLAY

logger = logging.getLogger(__name__)
_PAGE_SIZE = 199
_HTTP_TIMEOUT_SECONDS = 30
_COVERAGE_WARNING = (
    "Only the public reviews retrievable for this storefront and query language "
    "were requested; this does not establish complete historical coverage."
)


class _PageFormatError(ValueError):
    """The public endpoint changed shape or returned an invalid payload."""


class _RateLimitedError(RuntimeError):
    """Google returned its gateway error instead of a review page."""


def _raw_to_normalized(raw, *, source_url, app_id, country, language, scraped_at):
    return make_review(
        platform=PLATFORM_GOOGLE_PLAY, source_url=source_url, app_id=app_id,
        country=country, language=detect_language(raw.get("content")),
        query_language=language, reviewer_country=None,
        review_id=raw.get("reviewId"), rating=raw.get("score"), title=None,
        text=raw.get("content"), reviewer_name=raw.get("userName"),
        review_date=raw.get("at"),
        app_version=raw.get("reviewCreatedVersion") or raw.get("appVersion"),
        developer_response=raw.get("replyContent"),
        developer_response_date=raw.get("repliedAt"), scraped_at=scraped_at,
    )


def _strict_page(app_id, *, country, language, sort, count, token):
    """One page with a timeout, verified TLS, and no swallowed exceptions.

    google-play-scraper 1.2.7 is pinned because its private format/specification
    objects are used here. Explicit TLS verification is needed because the
    dependency changes the global default TLS context.
    """
    from google_play_scraper.constants.element import ElementSpecs
    from google_play_scraper.constants.regex import Regex
    from google_play_scraper.constants.request import Formats

    request = Request(
        Formats.Reviews.build(lang=language, country=country),
        data=Formats.Reviews.build_body(app_id, sort.value, count, "null", "null", token),
        headers={"content-type": "application/x-www-form-urlencoded"},
    )
    with urlopen(request, timeout=_HTTP_TIMEOUT_SECONDS, context=ssl.create_default_context()) as response:
        dom = response.read().decode("utf-8")
    if "com.google.play.gateway.proto.PlayGatewayError" in dom:
        raise _RateLimitedError("Google Play gateway error; the page was not collected")
    try:
        matches = Regex.REVIEWS.findall(dom)
        if not matches:
            raise _PageFormatError("Google Play response has no review envelope")
        envelope = json.loads(matches[0])
        payload = json.loads(envelope[0][2])
        if not isinstance(payload, list):
            raise _PageFormatError("Google Play review payload is not a list")
        if not payload:
            return [], None
        items = payload[0]
        if not isinstance(items, list):
            raise _PageFormatError("Google Play review items are not a list")
        next_token = payload[-2][-1] if len(payload) >= 2 and payload[-2] else None
        if next_token == []:
            next_token = None
        if next_token is not None and not isinstance(next_token, str):
            raise _PageFormatError("Google Play returned an invalid continuation token")
        reviews = []
        for item in items:
            if not isinstance(item, list):
                raise _PageFormatError("Google Play returned an invalid review item")
            raw = {field: spec.extract_content(item) for field, spec in ElementSpecs.Review.items()}
            if not raw.get("reviewId"):
                raise _PageFormatError("Google Play review is missing its identifier")
            reviews.append(raw)
        return reviews, next_token or None
    except (IndexError, TypeError, json.JSONDecodeError) as exc:
        raise _PageFormatError("Could not parse the Google Play review response") from exc


def _is_transient(exc):
    if isinstance(exc, HTTPError):
        return exc.code in (408, 425, 429) or 500 <= exc.code <= 599
    return isinstance(exc, (URLError, TimeoutError, ConnectionError, OSError, _RateLimitedError))


def _fetch_page(page_fn, app_id, *, retries, **kwargs):
    """Retry transient HTTP/network errors without hiding exhausted retries."""
    for attempt in range(max(0, retries) + 1):
        try:
            return page_fn(app_id, **kwargs)
        except Exception as exc:
            if attempt >= retries or not _is_transient(exc):
                raise
            delay = min(2 ** attempt, 8)
            logger.warning("Google Play page failed; retrying in %ss: %s", delay, exc)
            time.sleep(delay)


def _raw_key(raw):
    if raw.get("reviewId"):
        return str(raw["reviewId"])
    return hashlib.sha256(json.dumps(raw, sort_keys=True, default=str).encode("utf-8")).hexdigest()


def scrape_with_report(
    *, app_id: str, source_url: str, country: str, language: str = "en",
    max_reviews: Optional[int] = None, newest_first: bool = True,
    page_delay_ms: int = 200, max_retries: int = 4, resume: Optional[dict] = None,
    on_page: Optional[Callable] = None, should_stop: Optional[Callable[[], bool]] = None,
    retain_reviews: bool = True,
) -> ScrapeResult:
    """Fetch pages and say why the target stopped, without asserting completeness.

    max_reviews includes the resumed cursor's fetched count. on_page receives
    only new rows plus an opaque JSON cursor. An offset makes overfull pages
    resumable without dropping reviews when a limit falls inside a server page.
    Set retain_reviews=False with an on_page writer to keep saved rows out of
    memory; counters and outcome reporting remain independent of the result list.
    """
    from google_play_scraper import Sort

    if max_reviews is not None and max_reviews < 0:
        raise ValueError("max_reviews must be nonnegative or None")
    if not retain_reviews and on_page is None:
        raise ValueError("retain_reviews=False requires an on_page persistence callback")
    state = dict(resume or {})
    token = state.get("token")
    if token is not None and not isinstance(token, str):
        raise ValueError("Resume token must be a string or None")
    fetched = max(0, int(state.get("fetched", 0)))
    pages_fetched = max(0, int(state.get("pages_fetched", 0)))
    offset = max(0, int(state.get("offset", 0)))
    checkpoint = {
        "token": token, "fetched": fetched, "pages_fetched": pages_fetched,
        "offset": offset, "complete": bool(state.get("complete", False)),
    }
    results: List[dict] = []
    seen_reviews = set()
    seen_tokens = {token} if token is not None else set()
    warnings = [_COVERAGE_WARNING]
    scraped_at = utc_now_iso()
    sort = Sort.NEWEST if newest_first else Sort.MOST_RELEVANT
    delay = max(page_delay_ms, 0) / 1000.0

    def report(status, reason, error=None):
        return ScrapeResult(
            reviews=results, status=status, stop_reason=reason,
            pages_fetched=pages_fetched, checkpoint=checkpoint,
            error=str(error) if error is not None else None, warnings=list(warnings),
        )

    if checkpoint["complete"]:
        return report("finished", "endpoint_end")
    while True:
        if should_stop and should_stop():
            return report("cancelled", "cancelled")
        remaining = None if max_reviews is None else max_reviews - fetched
        if remaining is not None and remaining <= 0:
            return report("limited", "review_limit")
        count = _PAGE_SIZE if remaining is None else min(_PAGE_SIZE, remaining + offset)
        try:
            batch, next_token = _fetch_page(
                _strict_page, app_id, country=country, language=language,
                sort=sort, count=count, token=token, retries=max_retries,
            )
        except Exception as exc:
            return report("partial" if fetched else "failed", "fetch_failed", exc)

        pages_fetched += 1
        combined = list(batch)
        page_seen = {_raw_key(raw) for raw in combined}
        recovery_error = None
        cancelled = False
        suspicious = next_token is None and len(batch) >= count
        # Retry the SAME input cursor on ambiguous exhaustion. Keep all reviews
        # recovered on retries, even if the contents changed in the meantime.
        if suspicious and (remaining is None or len(batch[offset:]) < remaining):
            for attempt in range(max(0, max_retries)):
                if should_stop and should_stop():
                    cancelled = True
                    break
                time.sleep(min(2 ** attempt, 8))
                try:
                    recovered, recovered_token = _fetch_page(
                        _strict_page, app_id, country=country, language=language,
                        sort=sort, count=count, token=token, retries=0,
                    )
                except Exception as exc:
                    recovery_error = exc
                    if not _is_transient(exc):
                        break
                    continue
                for raw in recovered:
                    key = _raw_key(raw)
                    if key not in page_seen:
                        page_seen.add(key)
                        combined.append(raw)
                if recovered_token is not None:
                    next_token = recovered_token
                    suspicious = False
                    break

        new_rows = []
        consumed = offset
        for raw in combined[offset:]:
            key = _raw_key(raw)
            consumed += 1
            if key in seen_reviews:
                continue
            if remaining is not None and len(new_rows) >= remaining:
                consumed -= 1
                break
            seen_reviews.add(key)
            new_rows.append(_raw_to_normalized(
                raw, source_url=source_url, app_id=app_id, country=country,
                language=language, scraped_at=scraped_at,
            ))

        fetched += len(new_rows)
        if retain_reviews:
            results.extend(new_rows)
        limit_inside_page = consumed < len(combined)
        repeated_cursor = next_token is not None and next_token in seen_tokens
        no_progress = not new_rows and not offset and (bool(combined) or next_token is not None)
        ambiguous = suspicious or repeated_cursor or no_progress
        proposed = {
            "token": token if limit_inside_page or ambiguous else next_token,
            "fetched": fetched, "pages_fetched": pages_fetched,
            "offset": consumed if limit_inside_page or ambiguous else 0,
            "complete": next_token is None and not ambiguous and not limit_inside_page,
        }
        try:
            if on_page:
                on_page(new_rows, dict(proposed))
        except Exception as exc:
            return report("partial", "checkpoint_failed", exc)
        checkpoint = proposed

        if cancelled:
            return report("cancelled", "cancelled")
        if max_reviews is not None and fetched >= max_reviews:
            return report("limited", "review_limit")
        if repeated_cursor:
            warnings.append("Google Play repeated a continuation cursor; collection stopped to avoid a loop.")
            return report("partial", "repeated_cursor")
        if no_progress:
            warnings.append("Google Play returned no new review identifiers; collection stopped to avoid a loop.")
            return report("partial", "no_progress")
        if suspicious:
            warnings.append("A full page had no continuation cursor; the endpoint may have cut off pagination.")
            return report("partial", "suspected_cutoff", recovery_error)
        if next_token is None:
            return report("finished", "empty_feed" if not combined and not fetched else "endpoint_end")
        seen_tokens.add(next_token)
        token, offset = next_token, 0
        if delay:
            time.sleep(delay)


def scrape(**kwargs) -> List[Dict[str, Any]]:
    """Compatibility entry point; use scrape_with_report for coverage status."""
    result = scrape_with_report(**kwargs)
    if result.status == "failed":
        raise RuntimeError(result.error or result.stop_reason)
    return result.reviews

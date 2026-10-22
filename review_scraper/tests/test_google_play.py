"""Google pagination regressions use endpoint-shaped payloads and no network."""
import importlib
import io
import json
from urllib.error import HTTPError, URLError

import pytest

from stores import google_play as gp


@pytest.fixture(autouse=True)
def no_wait_or_language_detection(monkeypatch):
    monkeypatch.setattr(gp.time, "sleep", lambda _: None)
    monkeypatch.setattr(gp, "detect_language", lambda _: None)


def collect(**kwargs):
    return gp.scrape_with_report(
        app_id="example.app", source_url="https://play.google.com/store/apps/details?id=example.app",
        country="pt", language="pt", page_delay_ms=0, **kwargs,
    )


def raw(identifier):
    return {"reviewId": str(identifier), "content": "Review text", "score": 5}


def endpoint_body(ids, token=None):
    # The pinned dependency's expected RPC envelope and positional review fields.
    items = [[str(identifier), ["Alice"], 5, None, "Review text", [1700000000]] for identifier in ids]
    payload = [items, [token], None]
    return (")]}\'\n\n" + json.dumps([["wrb.fr", "oCPfdb", json.dumps(payload)]])).encode()


class Response(io.BytesIO):
    pass


def test_real_dependency_swallow_regression_is_reported_and_retried(monkeypatch):
    """Reproduce the dependency behavior, then exercise our real HTTP boundary."""
    dependency = importlib.import_module("google_play_scraper.features.reviews")

    def fail(*args, **kwargs):
        raise URLError("network down")

    monkeypatch.setattr(dependency, "_fetch_review_items", fail)
    rows, cursor = dependency.reviews("example.app", count=199)
    assert rows == [] and cursor.token is None  # Old code mistook this for success.
    attempts = []

    def failed_http(*args, **kwargs):
        attempts.append(kwargs)
        raise URLError("network down")

    monkeypatch.setattr(gp, "urlopen", failed_http)
    result = collect(max_retries=2)
    assert result.status == "failed"
    assert result.stop_reason == "fetch_failed"
    assert "network down" in result.error
    assert result.reviews == [] and len(attempts) == 3
    assert all(attempt["timeout"] == 30 for attempt in attempts)
    assert all(attempt["context"].verify_mode.name == "CERT_REQUIRED" for attempt in attempts)


def test_strict_http_recovers_and_preserves_storefront_and_unknown_language(monkeypatch):
    responses = [URLError("temporary"), endpoint_body(["one"])]

    def http(*args, **kwargs):
        item = responses.pop(0)
        if isinstance(item, Exception):
            raise item
        return Response(item)

    monkeypatch.setattr(gp, "urlopen", http)
    result = collect(max_retries=1)
    assert result.status == "finished" and result.stop_reason == "endpoint_end"
    assert result.reviews[0]["country"] == "pt"
    assert result.reviews[0]["reviewer_country"] is None
    assert result.reviews[0]["language"] is None
    assert result.reviews[0]["query_language"] == "pt"


@pytest.mark.parametrize("status,attempts", [(404, 1), (429, 3), (503, 3)])
def test_http_permanent_errors_not_retried_but_transient_errors_are(monkeypatch, status, attempts):
    calls = []

    def http(*args, **kwargs):
        calls.append(1)
        raise HTTPError("https://play.google.com", status, "failed", {}, None)

    monkeypatch.setattr(gp, "urlopen", http)
    result = collect(max_retries=2)
    assert result.status == "failed" and len(calls) == attempts


def test_invalid_payload_is_failure_instead_of_empty_feed(monkeypatch):
    monkeypatch.setattr(gp, "urlopen", lambda *a, **k: Response(b"<html>blocked</html>"))
    result = collect()
    assert result.status == "failed" and result.stop_reason == "fetch_failed"
    assert "envelope" in result.error


def test_later_failure_preserves_pages_and_safe_resume_cursor(monkeypatch):
    calls, committed = [], []

    def page(app, **kwargs):
        calls.append(kwargs["token"])
        if kwargs["token"] is None:
            return [raw("first")], "next"
        raise URLError("second page failed")

    monkeypatch.setattr(gp, "_strict_page", page)
    result = collect(max_retries=1, on_page=lambda rows, cursor: committed.append((rows, cursor)))
    assert result.status == "partial" and result.stop_reason == "fetch_failed"
    assert [r["review_id"] for r in result.reviews] == ["first"]
    assert calls == [None, "next", "next"]
    assert len(committed) == 1 and result.checkpoint == committed[0][1]
    assert result.checkpoint["token"] == "next" and result.checkpoint["fetched"] == 1
    assert result.pages_fetched == 1
    json.dumps(result.checkpoint)

    monkeypatch.setattr(gp, "_strict_page", lambda app, **kw: ([raw("second")], None))
    resumed = collect(resume=result.checkpoint)
    assert resumed.status == "finished"
    assert [r["review_id"] for r in resumed.reviews] == ["second"]
    assert resumed.checkpoint["fetched"] == 2


def test_full_page_missing_cursor_keeps_all_recovered_rows(monkeypatch):
    first = [raw(i) for i in range(199)]
    retry = [raw(i) for i in range(1, 200)]
    pages = [(first, None), (retry, "next"), ([raw(200)], None)]
    tokens = []

    def page(app, **kwargs):
        tokens.append(kwargs["token"])
        return pages.pop(0)

    monkeypatch.setattr(gp, "_strict_page", page)
    result = collect(max_retries=1)
    assert result.status == "finished" and len(result.reviews) == 201
    assert len({r["review_id"] for r in result.reviews}) == 201
    assert tokens == [None, None, "next"]


def test_full_page_without_cursor_reports_suspected_cutoff(monkeypatch):
    monkeypatch.setattr(gp, "_strict_page", lambda app, **kw: ([raw(i) for i in range(199)], None))
    result = collect(max_retries=1)
    assert len(result.reviews) == 199
    assert result.status == "partial" and result.stop_reason == "suspected_cutoff"
    assert result.checkpoint["complete"] is False
    assert result.checkpoint["token"] is None and result.checkpoint["offset"] == 199


def test_cutoff_retry_failure_keeps_successfully_collected_page(monkeypatch):
    calls = []

    def page(app, **kwargs):
        calls.append(1)
        if len(calls) == 1:
            return [raw(i) for i in range(199)], None
        raise URLError("retry failed")

    monkeypatch.setattr(gp, "_strict_page", page)
    result = collect(max_retries=2)
    assert result.status == "partial" and len(result.reviews) == 199
    assert result.stop_reason == "suspected_cutoff" and "retry failed" in result.error
    assert len(calls) == 3


def test_user_limit_and_overfull_page_resume_do_not_skip_rows(monkeypatch):
    monkeypatch.setattr(gp, "_strict_page", lambda app, **kw: ([raw(1), raw(2), raw(3)], "next"))
    limited = collect(max_reviews=2)
    assert limited.status == "limited" and len(limited.reviews) == 2
    assert limited.checkpoint["offset"] == 2
    tokens = []

    def page(app, **kwargs):
        tokens.append(kwargs["token"])
        return ([raw(1), raw(2), raw(3)], "next") if kwargs["token"] is None else ([], None)

    monkeypatch.setattr(gp, "_strict_page", page)
    resumed = collect(resume=limited.checkpoint)
    assert resumed.status == "finished"
    assert [r["review_id"] for r in resumed.reviews] == ["3"]
    assert tokens == [None, "next"] and resumed.checkpoint["fetched"] == 3


def test_repeated_cursor_stops_with_partial_results(monkeypatch):
    pages = iter([([raw(1)], "same"), ([raw(2)], "same")])
    monkeypatch.setattr(gp, "_strict_page", lambda *a, **kw: next(pages))
    result = collect()
    assert result.status == "partial" and result.stop_reason == "repeated_cursor"
    assert len(result.reviews) == 2 and result.pages_fetched == 2


@pytest.mark.parametrize("second", [[], [raw(1)]])
def test_no_progress_stops_even_when_endpoint_issues_new_cursors(monkeypatch, second):
    pages = iter([([raw(1)], "a"), (second, "b")])
    monkeypatch.setattr(gp, "_strict_page", lambda *a, **kw: next(pages))
    result = collect()
    assert result.status == "partial" and result.stop_reason == "no_progress"
    assert len(result.reviews) == 1


def test_empty_feed_is_distinct_from_failed_fetch(monkeypatch):
    monkeypatch.setattr(gp, "urlopen", lambda *a, **k: Response(endpoint_body([])))
    result = collect()
    assert result.status == "finished" and result.stop_reason == "empty_feed"
    assert result.checkpoint["complete"] is True


def test_checkpoint_failure_does_not_advance_cursor(monkeypatch):
    monkeypatch.setattr(gp, "_strict_page", lambda *a, **kw: ([raw(1)], "next"))

    def broken_storage(rows, cursor):
        raise OSError("disk full")

    result = collect(on_page=broken_storage)
    assert result.status == "partial" and result.stop_reason == "checkpoint_failed"
    assert result.checkpoint["token"] is None and result.checkpoint["fetched"] == 0
    assert "disk full" in result.error and len(result.reviews) == 1


def test_streaming_collection_keeps_rows_in_callback_and_reports_partial_failure(monkeypatch):
    saved = []

    def page(app, **kwargs):
        if kwargs["token"] is None:
            return [raw(1), raw(2)], "next"
        raise URLError("later page failed")

    monkeypatch.setattr(gp, "_strict_page", page)
    result = collect(
        retain_reviews=False, max_retries=0,
        on_page=lambda rows, cursor: saved.extend(rows),
    )
    assert result.reviews == []
    assert [row["review_id"] for row in saved] == ["1", "2"]
    assert result.status == "partial" and result.stop_reason == "fetch_failed"
    assert result.checkpoint["fetched"] == 2 and result.checkpoint["token"] == "next"


def test_streaming_collection_requires_callback_and_enforces_limit(monkeypatch):
    with pytest.raises(ValueError, match="on_page"):
        collect(retain_reviews=False)
    monkeypatch.setattr(gp, "_strict_page", lambda *a, **kw: ([raw(1), raw(2)], "next"))
    saved = []
    result = collect(retain_reviews=False, max_reviews=2, on_page=lambda rows, c: saved.extend(rows))
    assert result.reviews == [] and len(saved) == 2
    assert result.status == "limited" and result.checkpoint["fetched"] == 2


def test_cancellation_and_zero_limit_do_not_fetch(monkeypatch):
    def unexpected(*args, **kwargs):
        pytest.fail("endpoint should not be called")

    monkeypatch.setattr(gp, "_strict_page", unexpected)
    assert collect(should_stop=lambda: True).status == "cancelled"
    assert collect(max_reviews=0).status == "limited"


def test_compatibility_entry_point_returns_list_and_raises_initial_failure(monkeypatch):
    monkeypatch.setattr(gp, "_strict_page", lambda *a, **kw: ([], None))
    assert gp.scrape(app_id="x", source_url="x", country="us") == []

    def broken(*args, **kwargs):
        raise URLError("offline")

    monkeypatch.setattr(gp, "_strict_page", broken)
    with pytest.raises(RuntimeError, match="offline"):
        gp.scrape(app_id="x", source_url="x", country="us", max_retries=0)

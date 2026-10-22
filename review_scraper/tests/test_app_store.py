"""Apple pagination is tested at the actual page boundary used for persistence."""

import json
import re
from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from app_store_web_scraper import AppStoreEntry

from stores import app_store


def _reviews(start, count):
    return [
        {
            "id": {"label": str(index)},
            "updated": {"label": "2026-01-02T03:04:05Z"},
            "author": {"name": {"label": "Reviewer"}},
            "title": {"label": "Useful app"},
            "content": {"label": "This app works well for me."},
            "im:rating": {"label": "5"},
            "im:version": {"label": "1.2"},
        }
        for index in range(start, start + count)
    ]


class _Session:
    def __init__(self, pages):
        self.pages = pages
        self.requested = []
        self._http = SimpleNamespace(clear=Mock())

    def _get(self, path):
        page = int(re.search(r"page=(\d+)", path)[1])
        self.requested.append(page)
        entries = self.pages[page]
        if isinstance(entries, Exception):
            raise entries
        return {"feed": {"link": [{"attributes": {"rel": "self"}}], "entry": entries}}


def _use_pages(monkeypatch, pages):
    session = _Session(pages)
    entry = AppStoreEntry(app_id=123, country="us", session=session)
    monkeypatch.setattr(app_store, "_new_entry", lambda *args: entry)
    monkeypatch.setattr(app_store, "detect_language", lambda text: "en")
    return session


def _scrape(**kwargs):
    return app_store.scrape_with_report(
        app_id="123", source_url="https://apps.apple.com/us/app/x/id123", country="us", **kwargs
    )


def test_later_page_failure_preserves_rows_and_resumable_checkpoint(monkeypatch):
    session = _use_pages(monkeypatch, {1: _reviews(1, 50), 2: TimeoutError("read timed out")})
    saved = []
    outcome = _scrape(on_page=lambda rows, checkpoint: saved.append((rows, checkpoint)))

    assert outcome.status == "partial"
    assert outcome.stop_reason == "error"
    assert "timed out" in outcome.error
    assert len(outcome.reviews) == 50
    assert session.requested == [1, 2]
    assert len(saved) == 1
    assert len(saved[0][0]) == 50
    assert outcome.checkpoint == saved[0][1]
    assert outcome.checkpoint["next_page"] == 2
    assert outcome.checkpoint["fetched"] == 50
    session._http.clear.assert_called_once()


def test_streamed_rows_are_persisted_without_retention_and_partial_status_survives(monkeypatch):
    _use_pages(monkeypatch, {1: _reviews(1, 50), 2: TimeoutError("read timed out")})
    persisted = []
    outcome = _scrape(
        retain_reviews=False,
        on_page=lambda rows, checkpoint: persisted.extend(rows),
    )

    assert len(persisted) == 50
    assert outcome.reviews == []
    assert outcome.checkpoint["fetched"] == 50
    assert outcome.checkpoint["next_page"] == 2
    assert outcome.status == "partial"
    assert outcome.stop_reason == "error"
    assert "timed out" in outcome.error


def test_disabling_retention_requires_persistence_callback():
    with pytest.raises(ValueError, match="on_page is required"):
        _scrape(retain_reviews=False)


def test_callbacks_persist_real_pages_and_feed_never_exceeds_500(monkeypatch):
    session = _use_pages(monkeypatch, {page: _reviews((page - 1) * 50, 50) for page in range(1, 11)})
    checkpoints = []
    outcome = _scrape(max_reviews=1000, on_page=lambda rows, cp: checkpoints.append((len(rows), cp)))

    assert len(outcome.reviews) == 500
    assert outcome.status == "limited"
    assert outcome.stop_reason == "feed_window_limit"
    assert session.requested == list(range(1, 11))
    assert [count for count, _ in checkpoints] == [50] * 10
    assert [cp["next_page"] for _, cp in checkpoints] == list(range(2, 12))
    assert outcome.checkpoint["fetched"] == 500
    assert outcome.pages_fetched == 10


def test_resume_requests_next_page_without_replaying_saved_page(monkeypatch):
    session = _use_pages(monkeypatch, {2: _reviews(51, 3), 3: []})
    outcome = _scrape(resume={"next_page": 2, "fetched": 50, "pages_fetched": 1})

    assert session.requested == [2, 3]
    assert [row["review_id"] for row in outcome.reviews] == ["51", "52", "53"]
    assert outcome.checkpoint["fetched"] == 53
    assert outcome.pages_fetched == 3
    assert outcome.status == "finished"
    assert outcome.stop_reason == "endpoint_end"


def test_requested_limit_keeps_remaining_page_available_for_resume(monkeypatch):
    _use_pages(monkeypatch, {1: _reviews(1, 50), 2: _reviews(51, 50)})
    first = _scrape(max_reviews=75)

    assert first.stop_reason == "requested_limit"
    assert len(first.reviews) == 75
    assert first.checkpoint["next_page"] == 2
    assert first.checkpoint["page_offset"] == 25

    session = _use_pages(monkeypatch, {2: _reviews(51, 50), 3: []})
    second = _scrape(resume=first.checkpoint)

    assert session.requested == [2, 3]
    assert [row["review_id"] for row in second.reviews] == [str(n) for n in range(76, 101)]
    assert second.checkpoint["fetched"] == 100


def test_persistence_failure_does_not_advance_past_unsaved_page(monkeypatch):
    session = _use_pages(monkeypatch, {1: _reviews(1, 50), 2: _reviews(51, 50)})
    calls = []

    def save(rows, checkpoint):
        calls.append(checkpoint)
        if len(calls) == 2:
            raise OSError("database full")

    outcome = _scrape(on_page=save)

    assert session.requested == [1, 2]
    assert len(calls) == 2
    assert len(outcome.reviews) == 50
    assert outcome.checkpoint["next_page"] == 2
    assert outcome.checkpoint["fetched"] == 50
    assert outcome.error == "database full"


def test_first_page_empty_is_reported_without_claiming_historical_completeness(monkeypatch):
    _use_pages(monkeypatch, {1: []})
    outcome = _scrape()

    assert outcome.status == "finished"
    assert outcome.stop_reason == "empty_feed"
    assert outcome.reviews == []
    assert "historical coverage" in outcome.warnings[0]


def test_initial_failure_and_cancellation_are_explicit(monkeypatch):
    session = _use_pages(monkeypatch, {1: ConnectionError("unavailable")})
    failed = _scrape()
    assert failed.status == "failed"
    assert failed.stop_reason == "error"
    assert failed.checkpoint["next_page"] == 1

    cancelled = _scrape(should_stop=lambda: True)
    assert cancelled.status == "cancelled"
    assert cancelled.stop_reason == "cancelled"
    assert session.requested == [1]


def test_session_uses_timeouts_retry_policy_and_requested_pacing():
    entry = app_store._new_entry("123", "us", 700, 2)
    try:
        options = entry._session._http.connection_pool_kw
        assert options["timeout"].connect_timeout == 10
        assert options["timeout"].read_timeout == 30
        assert options["retries"].total == 2
        assert 429 in options["retries"].status_forcelist
        assert 503 in options["retries"].status_forcelist
        assert entry._session._delay == 0.7
    finally:
        entry._session._http.clear()


def test_response_object_becomes_serializable_text_and_modified_date(monkeypatch):
    modified = datetime(2026, 1, 3, 4, 5, tzinfo=timezone.utc)
    review = SimpleNamespace(
        id=123,
        content="This app works well for me.",
        developer_response=SimpleNamespace(body="Thank you for your feedback.", modified=modified),
    )
    monkeypatch.setattr(app_store, "detect_language", lambda text: "en")
    normalized = app_store._review_to_normalized(
        review, source_url="u", app_id="1", country="us", scraped_at="2026-01-04T00:00:00Z"
    )

    assert normalized["developer_response"] == "Thank you for your feedback."
    assert normalized["developer_response_date"] == modified.isoformat()
    assert json.loads(json.dumps(normalized))["developer_response"] == "Thank you for your feedback."


def test_string_response_and_explicit_response_date_are_preserved(monkeypatch):
    review = SimpleNamespace(
        content="Useful app", developer_response="Thanks!", developer_response_date="2026-01-03"
    )
    monkeypatch.setattr(app_store, "detect_language", lambda text: "en")
    normalized = app_store._review_to_normalized(
        review, source_url="u", app_id="1", country="us", scraped_at="2026-01-04T00:00:00Z"
    )
    assert normalized["developer_response"] == "Thanks!"
    assert normalized["developer_response_date"] == "2026-01-03"


def test_entry_setup_failure_closes_newly_created_session(monkeypatch):
    import app_store_web_scraper

    session = SimpleNamespace(_http=SimpleNamespace(connection_pool_kw={}, clear=Mock()))
    monkeypatch.setattr(app_store_web_scraper, "AppStoreSession", lambda **kwargs: session)
    with pytest.raises(KeyError):
        app_store._new_entry("123", "us", 600, 1)
    session._http.clear.assert_called_once()


@pytest.mark.parametrize("checkpoint", [{"next_page": 0}, {"fetched": 501}, {"page_offset": 50}])
def test_invalid_resume_checkpoint_is_rejected(checkpoint):
    with pytest.raises(ValueError, match="checkpoint"):
        _scrape(resume=checkpoint)

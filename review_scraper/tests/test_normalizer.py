import sys
from datetime import datetime
from types import SimpleNamespace

import normalizer
from normalizer import REVIEW_FIELDS, detect_language, infer_country, make_review, to_iso


def test_make_review_has_all_schema_fields():
    review = make_review(
        platform="google_play",
        source_url="https://play.google.com/store/apps/details?id=com.example.app",
        app_id="com.example.app",
        country="us",
        language="en",
        review_id="abc123",
        rating="5",
        text="Great app",
        reviewer_name="Alice",
        review_date=datetime(2024, 1, 2, 3, 4, 5),
    )
    for field in REVIEW_FIELDS:
        assert field in review
    # rating is coerced to int.
    assert review["rating"] == 5
    # datetime is rendered ISO.
    assert review["review_date"].startswith("2024-01-02")
    # scraped_at is auto-populated.
    assert review["scraped_at"]


def test_make_review_handles_missing_optionals():
    review = make_review(
        platform="app_store",
        source_url="https://apps.apple.com/us/app/x/id1",
        app_id="1",
        country="us",
    )
    assert review["title"] is None
    assert review["rating"] is None
    assert review["language"] is None
    assert review["query_language"] is None
    assert review["reviewer_country"] is None
    assert review["developer_response"] is None


def test_rating_non_numeric_becomes_none():
    review = make_review(
        platform="app_store",
        source_url="u",
        app_id="1",
        country="us",
        rating="not-a-number",
    )
    assert review["rating"] is None


def test_to_iso_variants():
    assert to_iso(None) is None
    assert to_iso("") is None
    assert to_iso("2024-05-01") == "2024-05-01"
    assert to_iso(datetime(2024, 5, 1)).startswith("2024-05-01")


def test_query_context_is_separate_from_detected_language_and_reviewer_location():
    review = make_review(
        platform="google_play", source_url="u", app_id="app", country="gb",
        query_language="en", language="pt", text="Um aplicativo excelente",
    )
    assert review["country"] == "gb"
    assert review["query_language"] == "en"
    assert review["language"] == "pt"
    assert review["reviewer_country"] is None
    assert infer_country("en") is None
    assert infer_country("pt") is None


def test_language_detection_reuses_duplicate_text_without_growing_forever(monkeypatch):
    calls = []
    factory = SimpleNamespace(seed=None)

    def detect(text):
        calls.append(text)
        return "EN"

    monkeypatch.setitem(sys.modules, "langdetect", SimpleNamespace(DetectorFactory=factory, detect=detect))
    monkeypatch.setattr(normalizer, "_LANGDETECT_READY", False)
    detect_language.cache_clear()
    try:
        assert detect_language("A repeated review") == "en"
        assert detect_language("A repeated review") == "en"
        assert calls == ["A repeated review"]
        assert factory.seed == 0
        # New unique reviews eventually evict old entries instead of retaining
        # every review ever encountered by a long-running worker.
        for index in range(4096):
            assert detect_language(f"Another review {index}") == "en"
        assert detect_language.cache_info().currsize == 4096
        assert detect_language("A repeated review") == "en"
        assert calls.count("A repeated review") == 2
        assert detect_language(None) is None
        assert detect_language("?") is None
    finally:
        detect_language.cache_clear()

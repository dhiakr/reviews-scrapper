import os
import sqlite3
import tempfile

import pytest

from normalizer import REVIEW_FIELDS, compute_dedup_key, deduplicate, make_review
from storage import SQLiteReviewStore


def _review(**overrides):
    base = dict(
        platform="google_play",
        source_url="u",
        app_id="com.example.app",
        country="us",
        rating=5,
        title=None,
        text="Nice app",
        review_date="2024-01-01",
    )
    base.update(overrides)
    return make_review(**base)


def test_dedup_key_uses_review_id_when_present():
    r = _review(review_id="rid-1")
    assert compute_dedup_key(r) == "google_play|com.example.app|rid-1"


@pytest.mark.parametrize("platform", ["google_play", "app_store"])
def test_same_review_id_deduplicates_across_query_countries_and_languages(platform):
    a = _review(platform=platform, review_id="same", country="us", query_language="en")
    b = _review(platform=platform, review_id="same", country="pt", query_language="pt")
    assert compute_dedup_key(a) == compute_dedup_key(b)
    assert len(deduplicate([a, b])) == 1


def test_fallback_identity_keeps_storefront_and_app_scope():
    assert compute_dedup_key(_review(country="us")) != compute_dedup_key(_review(country="pt"))
    assert compute_dedup_key(_review(review_id="same")) != compute_dedup_key(
        _review(review_id="same", app_id="other.app")
    )


def test_dedup_key_hashes_when_no_review_id():
    r = _review()
    key = compute_dedup_key(r)
    assert key.startswith("google_play|com.example.app|us|h:")


def test_dedup_key_stable_for_same_content():
    assert compute_dedup_key(_review()) == compute_dedup_key(_review())


def test_dedup_key_differs_for_different_text():
    assert compute_dedup_key(_review(text="A")) != compute_dedup_key(_review(text="B"))


def test_deduplicate_removes_duplicates():
    reviews = [_review(review_id="x"), _review(review_id="x"), _review(review_id="y")]
    out = deduplicate(reviews)
    assert len(out) == 2


def test_deduplicate_keeps_latest_edit_and_latest_dated_response():
    older = _review(
        review_id="same", text="Original", scraped_at="2025-01-01T00:00:00+00:00",
        developer_response="Latest reply", developer_response_date="2025-01-04",
    )
    newer = _review(
        review_id="same", text="Edited", scraped_at="2025-01-02T00:00:00Z",
        developer_response="Cached reply", developer_response_date="2025-01-03",
    )
    for rows in ([older, newer], [newer, older]):
        merged = deduplicate(rows)[0]
        assert merged["text"] == "Edited"
        assert merged["developer_response"] == "Latest reply"


def test_changed_text_does_not_inherit_old_detected_language():
    older = _review(review_id="same", language="pt", scraped_at="2025-01-01")
    newer = _review(review_id="same", language=None, text="??", scraped_at="2025-01-02")
    assert deduplicate([older, newer])[0]["language"] is None


def test_sqlite_upsert_is_idempotent():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        reviews = [_review(review_id="a"), _review(review_id="b")]
        with SQLiteReviewStore(path) as store:
            first = store.upsert_many(reviews)
            assert first.inserted == 2
            # Second run with the same data inserts nothing new.
            second = store.upsert_many(reviews)
            assert second.inserted == 0
            assert second.updated == 2
            assert len(store.fetch_all()) == 2
    finally:
        os.remove(path)


def test_sqlite_adds_only_new_rows_on_repeat_run():
    fd, path = tempfile.mkstemp(suffix=".sqlite")
    os.close(fd)
    try:
        with SQLiteReviewStore(path) as store:
            store.upsert_many([_review(review_id="a")])
            result = store.upsert_many([_review(review_id="a"), _review(review_id="b")])
            assert result.inserted == 1
            assert result.updated == 1
            assert len(store.fetch_all()) == 2
    finally:
        os.remove(path)


def test_sqlite_updates_same_id_seen_in_different_storefront(tmp_path):
    with SQLiteReviewStore(str(tmp_path / "reviews.sqlite")) as store:
        store.upsert_many([_review(review_id="same", country="us", scraped_at="2025-01-01")])
        result = store.upsert_many([
            _review(review_id="same", country="pt", query_language="pt", text="Edited",
                    scraped_at="2025-01-02", developer_response="Fixed it"),
        ])
        assert result.inserted == 0
        assert result.updated == 1
        assert len(store.fetch_all()) == 1
        assert store.fetch_all()[0]["text"] == "Edited"
        assert store.fetch_all()[0]["query_language"] == "pt"


def test_sqlite_migrates_legacy_keys_and_unknown_google_country(tmp_path):
    path = str(tmp_path / "legacy.sqlite")
    legacy_fields = [field for field in REVIEW_FIELDS if field not in {"query_language", "reviewer_country"}]
    columns = ", ".join(f"{field} TEXT" for field in legacy_fields)
    with sqlite3.connect(path) as conn:
        conn.execute(
            f"CREATE TABLE reviews (id INTEGER PRIMARY KEY, dedup_key TEXT NOT NULL UNIQUE, "
            f"{columns}, created_at TEXT NOT NULL, updated_at TEXT NOT NULL)"
        )
        for i, (country, date, text, reply) in enumerate([
            ("us", "2025-01-01", "Original", "A useful response"),
            ("pt", "2025-01-02", "Edited", None),
        ], 1):
            review = _review(review_id="same", country=country, text=text,
                             scraped_at=date, developer_response=reply)
            values = [i, f"google_play|com.example.app|{country}|same"]
            values += [review[field] for field in legacy_fields] + [date, date]
            conn.execute(
                f"INSERT INTO reviews VALUES ({', '.join('?' for _ in values)})", values,
            )
    with SQLiteReviewStore(path) as store:
        rows = store.fetch_all()
        assert len(rows) == 1
        assert rows[0]["text"] == "Edited"
        assert rows[0]["developer_response"] == "A useful response"
        assert rows[0]["country"] is None
        assert rows[0]["query_language"] is None
        assert rows[0]["reviewer_country"] is None
        assert store.conn.execute("SELECT dedup_key FROM reviews").fetchone()[0] == (
            "google_play|com.example.app|same"
        )
    # Reopening runs no destructive migration and the archive remains editable.
    with SQLiteReviewStore(path) as store:
        result = store.upsert_many([_review(review_id="same", country="gb", text="Newest")])
        assert result.updated == 1
        assert store.fetch_all()[0]["country"] == "gb"


def test_sqlite_batch_rolls_back_if_iterator_fails(tmp_path):
    def broken_batch():
        yield _review(review_id="first")
        raise RuntimeError("interrupted")

    with SQLiteReviewStore(str(tmp_path / "reviews.sqlite")) as store:
        with pytest.raises(RuntimeError, match="interrupted"):
            store.upsert_many(broken_batch())
        assert store.fetch_all() == []

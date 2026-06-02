import os
import tempfile

from normalizer import compute_dedup_key, deduplicate, make_review
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
    assert compute_dedup_key(r) == "google_play|com.example.app|us|rid-1"


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

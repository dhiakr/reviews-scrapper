import sqlite3
from concurrent.futures import ThreadPoolExecutor

import pytest

from jobs import SQLiteJobStore
from normalizer import make_review


def _review(review_id="a", **overrides):
    fields = dict(
        platform="google_play", app_id="example.app", source_url="u",
        country="us", query_language="en", language=None, review_id=review_id,
        text="Original", review_date="2025-01-01", scraped_at="2025-01-01",
    )
    fields.update(overrides)
    return make_review(**fields)


def _targets():
    return [
        {"id": "us/en", "country": "us", "language": "en"},
        {"id": "pt/pt", "country": "pt", "language": "pt"},
    ]


def test_page_and_resume_checkpoint_survive_reopening(tmp_path):
    path = str(tmp_path / "jobs.sqlite")
    checkpoint = {"token": "opaque-cursor", "count": 1, "sort": 2}
    with SQLiteJobStore(path) as store:
        job_id = store.create_job({"app_id": "example.app"}, _targets())
        store.set_job_status(job_id, "running")
        store.save_page(job_id, "us/en", [_review()], checkpoint)
    with SQLiteJobStore(path) as store:
        job = store.get_job(job_id)
        assert job["config"] == {"app_id": "example.app"}
        assert job["status"] == "running"
        assert job["review_count"] == 1
        assert job["oldest_review_date"] == "2025-01-01"
        assert job["targets"][0]["checkpoint"] == checkpoint
        assert job["targets"][0]["pages_fetched"] == 1
        assert job["targets"][0]["raw_count"] == 1
        store.save_page(job_id, "us/en", [_review("b", review_date="2025-01-02")], {"done": True})
        assert store.get_job(job_id)["review_count"] == 2
        assert store.get_job(job_id)["newest_review_date"] == "2025-01-02"
        assert len(store.fetch_reviews(job_id, limit=1)) == 1
        assert store.fetch_reviews(job_id, limit=0) == []


def test_reviews_deduplicate_across_targets_but_are_scoped_to_job(tmp_path):
    with SQLiteJobStore(str(tmp_path / "jobs.sqlite")) as store:
        job_id = store.create_job({}, _targets())
        other_job = store.create_job({}, _targets())
        store.save_page(job_id, "us/en", [_review()], {"page": 1})
        store.save_page(job_id, "pt/pt", [
            _review(country="pt", query_language="pt", text="Edited", scraped_at="2025-01-02"),
        ], {"page": 1})
        job = store.get_job(job_id)
        assert job["review_count"] == 1
        assert [target["review_count"] for target in job["targets"]] == [1, 1]
        assert store.fetch_reviews(job_id)[0]["text"] == "Edited"
        assert store.fetch_reviews(other_job) == []
        assert store.list_jobs() == []
        assert [job["id"] for job in store.list_jobs([job_id, "missing", job_id])] == [job_id]
        store.save_page(other_job, "us/en", [_review(text="Independent")], {})
        assert store.fetch_reviews(other_job)[0]["text"] == "Independent"
        assert store.fetch_reviews(job_id)[0]["text"] == "Edited"


def test_page_failure_rolls_back_reviews_memberships_and_checkpoint(tmp_path):
    with SQLiteJobStore(str(tmp_path / "jobs.sqlite")) as store:
        job_id = store.create_job({}, _targets())
        store.save_page(job_id, "us/en", [_review()], {"page": 1})
        bad_review = _review("bad")
        bad_review["text"] = object()  # Fails serialization after the first row is written.
        with pytest.raises(TypeError):
            store.save_page(job_id, "us/en", [_review("b"), bad_review], {"page": 2})
        job = store.get_job(job_id)
        assert job["review_count"] == 1
        assert job["targets"][0]["checkpoint"] == {"page": 1}
        assert job["targets"][0]["pages_fetched"] == 1
        assert job["targets"][0]["raw_count"] == 1
        assert job["targets"][0]["review_count"] == 1


def test_target_state_is_persisted_with_allowlisted_fields(tmp_path):
    path = str(tmp_path / "jobs.sqlite")
    with SQLiteJobStore(path) as store:
        job_id = store.create_job({}, _targets())
        store.update_target(job_id, "us/en", status="partial", stop_reason="network_error",
                            error="temporary", warnings=["Uncertain end"], pages_fetched=4)
        store.set_job_status(job_id, "interrupted")
        with pytest.raises(ValueError, match="Unknown target fields"):
            store.update_target(job_id, "us/en", **{"status = 'finished' --": "bad"})
        with pytest.raises(ValueError, match="status"):
            store.set_job_status(job_id, "complete_everything")
    with SQLiteJobStore(path) as store:
        target = store.get_job(job_id)["targets"][0]
        assert target["status"] == "partial"
        assert target["pages_fetched"] == 4
        assert target["warnings"] == ["Uncertain end"]
        assert target["error"] == "temporary"
        assert store.get_job(job_id)["status"] == "interrupted"


def test_unknown_target_cannot_insert_unscoped_reviews(tmp_path):
    with SQLiteJobStore(str(tmp_path / "jobs.sqlite")) as store:
        job_id = store.create_job({}, _targets())
        with pytest.raises(KeyError):
            store.save_page(job_id, "missing", [_review()], {"page": 1})
        with pytest.raises(KeyError):
            store.fetch_reviews("missing")
        assert store.fetch_reviews(job_id) == []


def test_duplicate_target_definition_rolls_back_job(tmp_path):
    with SQLiteJobStore(str(tmp_path / "jobs.sqlite")) as store:
        with pytest.raises(sqlite3.IntegrityError):
            store.create_job({}, [_targets()[0], _targets()[0]])
        assert store.conn.execute("SELECT COUNT(*) FROM collection_jobs").fetchone()[0] == 0


def test_workers_use_separate_connections_without_losing_pages(tmp_path):
    path = str(tmp_path / "jobs.sqlite")
    with SQLiteJobStore(path) as store:
        job_id = store.create_job({}, _targets())

    def save(target_id, review_id):
        with SQLiteJobStore(path) as worker:
            worker.save_page(job_id, target_id, [_review(review_id)], {"done": True})

    with ThreadPoolExecutor(max_workers=2) as pool:
        list(pool.map(lambda args: save(*args), [("us/en", "a"), ("pt/pt", "b")]))
    with SQLiteJobStore(path) as store:
        assert store.get_job(job_id)["review_count"] == 2
        assert [target["pages_fetched"] for target in store.get_job(job_id)["targets"]] == [1, 1]

"""Integration regressions for partial jobs, recovery and scheduled archives."""

import json

import main
import runner
from jobs import SQLiteJobStore
from normalizer import make_review
from scrape_report import ScrapeResult
from storage import SQLiteReviewStore


URL = "https://play.google.com/store/apps/details?id=com.example.app"


def review(review_id, country="us", language="en"):
    return make_review(platform="google_play", source_url=URL, app_id="com.example.app",
                       country=country, query_language=language, review_id=review_id,
                       rating=4, text="Useful application", review_date="2026-10-01T12:00:00Z")


def test_partial_page_survives_worker_failure_and_resume_skips_finished_targets(tmp_path, monkeypatch):
    path = str(tmp_path / "jobs.sqlite3")
    config = dict(platform="google_play", app_id="com.example.app", source_url=URL)
    with SQLiteJobStore(path) as store:
        job_id = store.create_job(config, runner.build_targets("google_play", ["us", "pt"], ["en"]))
    calls = []

    def initial(**kwargs):
        calls.append(kwargs["country"])
        checkpoint = {"token": "next", "fetched": 1, "pages_fetched": 1}
        kwargs["on_page"]([review(kwargs["country"])], checkpoint)
        if kwargs["country"] == "us":
            raise RuntimeError("worker interrupted after a persisted page")
        return ScrapeResult([], "finished", "endpoint_end", 1, checkpoint)

    monkeypatch.setattr(runner.google_play, "scrape_with_report", initial)
    job = runner.run_job(path, job_id)
    assert job["status"] == "partial"
    assert job["review_count"] == 2
    assert calls == ["us", "pt"]
    assert job["targets"][0]["checkpoint"]["token"] == "next"

    def resumed(**kwargs):
        calls.append(kwargs["country"])
        assert kwargs["resume"]["token"] == "next"
        checkpoint = {"token": None, "complete": True, "fetched": 2, "pages_fetched": 2}
        kwargs["on_page"]([review("recovered")], checkpoint)
        return ScrapeResult([], "finished", "endpoint_end", 2, checkpoint)

    monkeypatch.setattr(runner.google_play, "scrape_with_report", resumed)
    job = runner.run_job(path, job_id)
    assert calls == ["us", "pt", "us"]
    assert job["status"] == "finished"
    assert job["review_count"] == 3


def test_overlapping_targets_have_one_review_but_separate_coverage(tmp_path, monkeypatch):
    path = str(tmp_path / "jobs.sqlite3")
    with SQLiteJobStore(path) as store:
        job_id = store.create_job(
            dict(platform="google_play", app_id="com.example.app", source_url=URL),
            runner.build_targets("google_play", ["us", "pt"], ["en", "pt"]),
        )

    def fetch(**kwargs):
        checkpoint = {"fetched": 1, "pages_fetched": 1, "complete": True}
        kwargs["on_page"]([review("shared", kwargs["country"], kwargs["language"])], checkpoint)
        return ScrapeResult([], "finished", "endpoint_end", 1, checkpoint)

    monkeypatch.setattr(runner.google_play, "scrape_with_report", fetch)
    job = runner.run_job(path, job_id)
    assert job["review_count"] == 1
    assert [target["review_count"] for target in job["targets"]] == [1, 1, 1, 1]


def test_cli_partial_exit_report_and_archive_saved_before_failure(tmp_path, monkeypatch):
    def fetch(**kwargs):
        checkpoint = {"token": "next", "fetched": 1, "pages_fetched": 1}
        kwargs["on_page"]([review("saved")], checkpoint)
        return ScrapeResult([], "partial", "fetch_failed", 1, checkpoint, "temporary error")

    monkeypatch.setattr(runner.google_play, "scrape_with_report", fetch)
    report = tmp_path / "coverage.json"
    archive = tmp_path / "archive.sqlite3"
    code = main.main(["scrape", URL, "--job-db", str(tmp_path / "jobs.sqlite3"),
                      "--db", str(archive), "--report", str(report),
                      "--out", str(tmp_path / "reviews.json")])
    assert code == 1
    assert json.loads(report.read_text())["targets"][0]["error"] == "temporary error"
    with SQLiteReviewStore(str(archive)) as store:
        assert [row["review_id"] for row in store.fetch_all()] == ["saved"]


def test_watch_upserts_archive_across_runs(tmp_path, monkeypatch):
    calls = []

    def fetch(**kwargs):
        calls.append(1)
        checkpoint = {"fetched": 1, "pages_fetched": 1, "complete": True}
        row = review("edited")
        row["text"] = f"Edit {len(calls)}"
        kwargs["on_page"]([row], checkpoint)
        return ScrapeResult([], "finished", "endpoint_end", 1, checkpoint)

    monkeypatch.setattr(runner.google_play, "scrape_with_report", fetch)
    monkeypatch.setattr(main.time, "sleep", lambda _: None)
    archive = tmp_path / "archive.sqlite3"
    code = main.main(["watch", URL, "--db", str(archive), "--job-db", str(tmp_path / "jobs.sqlite3"),
                      "--cycles", "2", "--interval", "3600"])
    assert code == 0
    with SQLiteReviewStore(str(archive)) as store:
        rows = store.fetch_all()
    assert len(rows) == 1
    assert rows[0]["text"] == "Edit 2"

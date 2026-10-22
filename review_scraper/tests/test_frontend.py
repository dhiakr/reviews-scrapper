"""Exercise actual Streamlit controls without making store requests."""

from pathlib import Path

from streamlit.testing.v1 import AppTest

import runner
from normalizer import make_review
from scrape_report import ScrapeResult


APP = Path(__file__).resolve().parents[2] / "streamlit_app.py"
URL = "https://play.google.com/store/apps/details?id=com.example.app"


def test_frontend_explicit_languages_and_saved_result(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW_SCRAPER_DATA_DIR", str(tmp_path))
    queried = []

    class SynchronousManager:
        def __init__(self, path):
            self.path = path

        def start(self, job_id):
            runner.run_job(self.path, job_id)
            return True

        def is_running(self, job_id):
            return False

        def error(self, job_id):
            return None

    def fetch(**kwargs):
        queried.append(kwargs["language"])
        rows = [make_review(platform="google_play", source_url=URL, app_id="com.example.app",
                            country=kwargs["country"], query_language=kwargs["language"],
                            language=None, review_id="same-review", text="OK", rating=5)]
        checkpoint = {"fetched": 1, "pages_fetched": 1, "complete": True}
        kwargs["on_page"](rows, checkpoint)
        return ScrapeResult([], "finished", "endpoint_end", 1, checkpoint)

    monkeypatch.setattr(runner, "JobManager", SynchronousManager)
    monkeypatch.setattr(runner.google_play, "scrape_with_report", fetch)
    app = AppTest.from_file(str(APP)).run()
    assert not app.exception
    app.text_input(key="url_input").set_value(URL).run()
    app.text_input(key="query_languages").set_value("en, pt")
    next(button for button in app.button if button.label == "Scrape reviews").click().run()
    assert not app.exception
    assert queried == ["en", "pt"]
    assert any(metric.label == "Unique reviews saved" and metric.value == "1" for metric in app.metric)
    assert any("finished" in subheader.value for subheader in app.subheader)
    next(button for button in app.button if button.label == "Prepare review download").click().run()
    assert not app.exception


def test_invalid_recovery_code_is_a_visible_error(tmp_path, monkeypatch):
    monkeypatch.setenv("REVIEW_SCRAPER_DATA_DIR", str(tmp_path))
    app = AppTest.from_file(str(APP)).run()
    next(field for field in app.text_input if field.label == "Recovery code").set_value("wrong")
    next(button for button in app.button if button.label == "Restore run").click().run()
    assert not app.exception
    assert any("Saved run not found" in error.value for error in app.error)

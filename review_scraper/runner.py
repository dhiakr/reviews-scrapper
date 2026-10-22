"""Run checkpointed collection jobs without depending on a web session."""

from __future__ import annotations

import logging
import threading
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from typing import Callable, Optional

from jobs import SQLiteJobStore
from stores import app_store, google_play
from url_parser import PLATFORM_GOOGLE_PLAY

logger = logging.getLogger(__name__)
_DONE = {"finished", "limited"}


def build_targets(platform, countries, languages=None):
    """Build explicit, unique storefront/language requests."""
    countries = list(dict.fromkeys(countries))
    languages = list(dict.fromkeys(languages or ["en"]))
    if platform == PLATFORM_GOOGLE_PLAY:
        return [
            {"id": f"{country}/{language}", "country": country, "language": language}
            for country in countries for language in languages
        ]
    return [{"id": country, "country": country, "language": None} for country in countries]


def _run_target(path, job_id, config, target, should_stop, on_page):
    """One target, one SQLite connection owned by its worker thread."""
    if should_stop():
        return
    with SQLiteJobStore(path) as store:
        target_id = target["id"]
        store.update_target(job_id, target_id, status="running", error=None)

        def save_page(rows, checkpoint):
            if on_page:
                on_page(rows)
            store.save_page(job_id, target_id, rows, checkpoint)

        kwargs = dict(
            app_id=config["app_id"], source_url=config["source_url"],
            country=target["country"], max_reviews=config.get("max_reviews"),
            page_delay_ms=config.get("page_delay_ms", 600),
            max_retries=config.get("max_retries", 5),
            resume=target.get("checkpoint") or None,
            on_page=save_page, should_stop=should_stop, retain_reviews=False,
        )
        adapter = app_store
        if config["platform"] == PLATFORM_GOOGLE_PLAY:
            adapter = google_play
            kwargs.update(language=target["language"],
                          newest_first=config.get("newest_first", True))
        try:
            result = adapter.scrape_with_report(**kwargs)
        except Exception as exc:
            logger.exception("Collection failed for %s", target_id)
            store.update_target(job_id, target_id, status="failed",
                                stop_reason="worker_error", error=str(exc))
            return
        store.update_target(
            job_id, target_id, status=result.status, stop_reason=result.stop_reason,
            error=result.error, pages_fetched=result.pages_fetched,
            warnings=result.warnings, checkpoint=result.checkpoint,
        )


def run_job(
    path: str,
    job_id: str,
    should_stop: Optional[Callable[[], bool]] = None,
    on_page=None,
):
    """Persist pages before advancing a cursor; retry only unfinished targets.

    ``on_page`` may run on several threads; any archive writer must open a
    connection on the calling thread. At most four targets run concurrently.
    """
    should_stop = should_stop or (lambda: False)
    with SQLiteJobStore(path) as store:
        job = store.get_job(job_id)
        config = job["config"]
        store.set_job_status(job_id, "running")
        stopped = threading.Event()

        def stop_requested():
            return stopped.is_set() or should_stop()

        try:
            workers = max(1, min(4, int(config.get("target_workers", 2))))
            with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="store-query") as executor:
                futures = [executor.submit(_run_target, path, job_id, config, target,
                                           stop_requested, on_page)
                           for target in job["targets"] if target["status"] not in _DONE]
                try:
                    for future in as_completed(futures):
                        future.result()
                except BaseException:
                    stopped.set()
                    for future in futures:
                        future.cancel()
                    raise
            job = store.get_job(job_id)
            statuses = [target["status"] for target in job["targets"]]
            if should_stop() or "cancelled" in statuses:
                status = "cancelled"
            elif all(status in _DONE for status in statuses):
                status = "finished"
            elif job.get("review_count", 0):
                status = "partial"
            else:
                status = "failed"
            store.set_job_status(job_id, status)
            return store.get_job(job_id)
        except BaseException:
            store.set_job_status(job_id, "interrupted")
            raise


class JobManager:
    """Bounded background workers; no Streamlit calls from worker threads."""

    def __init__(self, path: str, max_workers: int = 2):
        self.path = path
        self._executor = ThreadPoolExecutor(max_workers=max_workers,
                                            thread_name_prefix="review-scraper")
        self._lock = threading.Lock()
        self._futures: dict[str, Future] = {}
        self._stops: dict[str, threading.Event] = {}

    def start(self, job_id: str) -> bool:
        with self._lock:
            future = self._futures.get(job_id)
            if future and not future.done():
                return False
            stop = threading.Event()
            self._stops[job_id] = stop
            self._futures[job_id] = self._executor.submit(
                run_job, self.path, job_id, stop.is_set,
            )
            return True

    def is_running(self, job_id: str) -> bool:
        with self._lock:
            future = self._futures.get(job_id)
            return bool(future and not future.done())

    def cancel(self, job_id: str):
        with self._lock:
            if job_id in self._stops:
                self._stops[job_id].set()

    def error(self, job_id: str):
        with self._lock:
            future = self._futures.get(job_id)
            if future and future.done() and not future.cancelled():
                exc = future.exception()
                return str(exc) if exc else None
        return None

    def close(self):
        with self._lock:
            for stop in self._stops.values():
                stop.set()
        self._executor.shutdown(wait=True, cancel_futures=True)

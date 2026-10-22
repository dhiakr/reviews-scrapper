"""Durable, job-scoped collection results and atomic page checkpoints.

Each worker opens its own ``SQLiteJobStore``. A job UUID is an unguessable
recovery capability; callers must only expose jobs belonging to their session.
No method enumerates other sessions' jobs by default.
"""

from __future__ import annotations

import json
import sqlite3
import uuid
from typing import Any, Iterable, Optional

from normalizer import compute_dedup_key, merge_reviews, utc_now_iso


_STATUSES = {
    "queued", "pending", "running", "finished", "limited", "partial",
    "failed", "cancelled", "interrupted",
}
_TARGET_FIELDS = {
    "status", "checkpoint", "error", "stop_reason", "pages_fetched",
    "raw_count", "started_at", "finished_at", "oldest_review_date",
    "newest_review_date", "warnings",
}


def _json(value: Any) -> str:
    return json.dumps(value, ensure_ascii=False, allow_nan=False)


def _status(value: str) -> str:
    if value not in _STATUSES:
        raise ValueError(f"Unknown collection status: {value!r}")
    return value


class SQLiteJobStore:
    """Persist pages and their next cursor in one SQLite transaction.

    Deduplication is per job rather than per database. A review found through
    several targets is stored once, with separate target membership records.
    A second job starts with its own empty result set.
    """

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self.conn.execute("PRAGMA foreign_keys = ON")
        self._create_schema()

    def _create_schema(self) -> None:
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_jobs (
                    id TEXT PRIMARY KEY,
                    config_json TEXT NOT NULL,
                    status TEXT NOT NULL,
                    created_at TEXT NOT NULL,
                    updated_at TEXT NOT NULL
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_targets (
                    job_id TEXT NOT NULL REFERENCES collection_jobs(id) ON DELETE CASCADE,
                    id TEXT NOT NULL,
                    position INTEGER NOT NULL,
                    country TEXT,
                    language TEXT,
                    status TEXT NOT NULL DEFAULT 'pending',
                    checkpoint_json TEXT NOT NULL DEFAULT '{}',
                    error TEXT,
                    stop_reason TEXT,
                    pages_fetched INTEGER NOT NULL DEFAULT 0,
                    raw_count INTEGER NOT NULL DEFAULT 0,
                    started_at TEXT,
                    finished_at TEXT,
                    oldest_review_date TEXT,
                    newest_review_date TEXT,
                    warnings_json TEXT NOT NULL DEFAULT '[]',
                    updated_at TEXT NOT NULL,
                    PRIMARY KEY (job_id, id)
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_reviews (
                    id INTEGER PRIMARY KEY AUTOINCREMENT,
                    job_id TEXT NOT NULL REFERENCES collection_jobs(id) ON DELETE CASCADE,
                    dedup_key TEXT NOT NULL,
                    review_json TEXT NOT NULL,
                    review_date TEXT,
                    UNIQUE (job_id, dedup_key)
                )
            """)
            self.conn.execute("""
                CREATE TABLE IF NOT EXISTS collection_target_reviews (
                    job_id TEXT NOT NULL,
                    target_id TEXT NOT NULL,
                    dedup_key TEXT NOT NULL,
                    PRIMARY KEY (job_id, target_id, dedup_key),
                    FOREIGN KEY (job_id, target_id)
                        REFERENCES collection_targets(job_id, id) ON DELETE CASCADE,
                    FOREIGN KEY (job_id, dedup_key)
                        REFERENCES collection_reviews(job_id, dedup_key) ON DELETE CASCADE
                )
            """)
            self.conn.execute(
                "CREATE INDEX IF NOT EXISTS idx_collection_reviews_dates "
                "ON collection_reviews (job_id, review_date)"
            )

    def create_job(self, config: dict, targets: list[dict]) -> str:
        config_json = _json(config)
        job_id = str(uuid.uuid4())
        now = utc_now_iso()
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self.conn.execute(
                "INSERT INTO collection_jobs (id, config_json, status, created_at, updated_at) "
                "VALUES (?, ?, 'queued', ?, ?)", (job_id, config_json, now, now),
            )
            for position, target in enumerate(targets):
                country = target.get("country")
                language = target.get("language")
                target_id = target.get("id") or "/".join(
                    str(value) for value in (country, language) if value
                ) or str(uuid.uuid4())
                self.conn.execute(
                    "INSERT INTO collection_targets "
                    "(job_id, id, position, country, language, updated_at) "
                    "VALUES (?, ?, ?, ?, ?, ?)",
                    (job_id, str(target_id), position, country, language, now),
                )
        return job_id

    def _require_job(self, job_id: str):
        row = self.conn.execute(
            "SELECT * FROM collection_jobs WHERE id = ?", (job_id,),
        ).fetchone()
        if row is None:
            raise KeyError("Collection job not found")
        return row

    def _require_target(self, job_id: str, target_id: str):
        row = self.conn.execute(
            "SELECT * FROM collection_targets WHERE job_id = ? AND id = ?",
            (job_id, target_id),
        ).fetchone()
        if row is None:
            raise KeyError("Collection target not found")
        return row

    def get_job(self, job_id: str) -> dict:
        row = self._require_job(job_id)
        job = dict(row)
        job["config"] = json.loads(job.pop("config_json"))
        totals = self.conn.execute(
            "SELECT COUNT(*) AS review_count, MIN(review_date) AS oldest_review_date, "
            "MAX(review_date) AS newest_review_date FROM collection_reviews WHERE job_id = ?",
            (job_id,),
        ).fetchone()
        job.update(dict(totals))
        job["targets"] = []
        for target_row in self.conn.execute(
            "SELECT t.*, (SELECT COUNT(*) FROM collection_target_reviews r "
            "WHERE r.job_id = t.job_id AND r.target_id = t.id) AS review_count "
            "FROM collection_targets t WHERE t.job_id = ? ORDER BY position", (job_id,),
        ):
            target = dict(target_row)
            target["checkpoint"] = json.loads(target.pop("checkpoint_json"))
            target["warnings"] = json.loads(target.pop("warnings_json"))
            job["targets"].append(target)
        return job

    def list_jobs(self, job_ids: Optional[Iterable[str]] = None) -> list[dict]:
        """Return only explicitly supplied recovery capabilities.

        An omitted/empty ID list returns no jobs, preventing accidental global
        browsing when the deployment is shared by several users.
        """
        jobs = []
        for job_id in dict.fromkeys(job_ids or []):
            try:
                jobs.append(self.get_job(job_id))
            except KeyError:
                continue
        return jobs

    def set_job_status(self, job_id: str, status: str) -> None:
        status = _status(status)
        with self.conn:
            updated = self.conn.execute(
                "UPDATE collection_jobs SET status = ?, updated_at = ? WHERE id = ?",
                (status, utc_now_iso(), job_id),
            )
            if updated.rowcount != 1:
                raise KeyError("Collection job not found")

    def update_target(self, job_id: str, target_id: str, **fields) -> None:
        unknown = set(fields) - _TARGET_FIELDS
        if unknown:
            raise ValueError(f"Unknown target fields: {', '.join(sorted(unknown))}")
        values = dict(fields)
        if "status" in values:
            values["status"] = _status(values["status"])
        for name in ("pages_fetched", "raw_count"):
            if name in values and (not isinstance(values[name], int) or values[name] < 0):
                raise ValueError(f"{name} must be a non-negative integer")
        for name in ("checkpoint", "warnings"):
            if name in values:
                values[f"{name}_json"] = _json(values.pop(name))
        values["updated_at"] = utc_now_iso()
        # Column names come only from the fixed allowlist above.
        assignments = ", ".join(f"{name} = ?" for name in values)
        with self.conn:
            updated = self.conn.execute(
                f"UPDATE collection_targets SET {assignments} WHERE job_id = ? AND id = ?",
                [*values.values(), job_id, target_id],
            )
            if updated.rowcount != 1:
                raise KeyError("Collection target not found")
            self.conn.execute(
                "UPDATE collection_jobs SET updated_at = ? WHERE id = ?",
                (values["updated_at"], job_id),
            )

    def save_page(self, job_id: str, target_id: str, reviews, checkpoint) -> None:
        """Commit review upserts and the next cursor together, or neither."""
        checkpoint_json = _json(checkpoint)
        now = utc_now_iso()
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            self._require_target(job_id, target_id)
            raw_count = 0
            for review in reviews:
                raw_count += 1
                key = compute_dedup_key(review)
                previous = self.conn.execute(
                    "SELECT review_json FROM collection_reviews WHERE job_id = ? AND dedup_key = ?",
                    (job_id, key),
                ).fetchone()
                if previous:
                    review = merge_reviews(json.loads(previous["review_json"]), review)
                self.conn.execute(
                    "INSERT INTO collection_reviews (job_id, dedup_key, review_json, review_date) "
                    "VALUES (?, ?, ?, ?) ON CONFLICT(job_id, dedup_key) DO UPDATE SET "
                    "review_json = excluded.review_json, review_date = excluded.review_date",
                    (job_id, key, _json(review), review.get("review_date")),
                )
                self.conn.execute(
                    "INSERT OR IGNORE INTO collection_target_reviews (job_id, target_id, dedup_key) "
                    "VALUES (?, ?, ?)", (job_id, target_id, key),
                )
            self.conn.execute(
                "UPDATE collection_targets SET checkpoint_json = ?, pages_fetched = pages_fetched + 1, "
                "raw_count = raw_count + ?, updated_at = ?, "
                "oldest_review_date = (SELECT MIN(r.review_date) FROM collection_reviews r "
                "JOIN collection_target_reviews tr ON tr.job_id = r.job_id AND tr.dedup_key = r.dedup_key "
                "WHERE tr.job_id = ? AND tr.target_id = ?), "
                "newest_review_date = (SELECT MAX(r.review_date) FROM collection_reviews r "
                "JOIN collection_target_reviews tr ON tr.job_id = r.job_id AND tr.dedup_key = r.dedup_key "
                "WHERE tr.job_id = ? AND tr.target_id = ?) WHERE job_id = ? AND id = ?",
                (checkpoint_json, raw_count, now, job_id, target_id, job_id, target_id, job_id, target_id),
            )
            self.conn.execute(
                "UPDATE collection_jobs SET updated_at = ? WHERE id = ?", (now, job_id),
            )

    def iter_reviews(self, job_id: str):
        """Stream an archive replay without loading the whole run into RAM."""
        self._require_job(job_id)
        for row in self.conn.execute(
            "SELECT review_json FROM collection_reviews WHERE job_id = ? ORDER BY id", (job_id,),
        ):
            yield json.loads(row["review_json"])

    def fetch_reviews(self, job_id: str, limit: Optional[int] = None) -> list[dict]:
        self._require_job(job_id)
        if limit is not None and (not isinstance(limit, int) or limit < 0):
            raise ValueError("limit must be a non-negative integer or None")
        sql = "SELECT review_json FROM collection_reviews WHERE job_id = ? ORDER BY id"
        parameters: list[Any] = [job_id]
        if limit is not None:
            sql += " LIMIT ?"
            parameters.append(limit)
        return [json.loads(row["review_json"]) for row in self.conn.execute(sql, parameters)]

    def close(self) -> None:
        self.conn.close()

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()

"""Storage layer.

An abstract ``ReviewStore`` interface plus a concrete SQLite implementation.
The interface is deliberately small and backend-agnostic so a Postgres adapter
can be dropped in later without touching the rest of the pipeline.
"""

from __future__ import annotations

import abc
import sqlite3
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List

from normalizer import REVIEW_FIELDS, compute_dedup_key, utc_now_iso


@dataclass
class UpsertResult:
    """Outcome of an upsert call."""

    inserted: int
    updated: int

    @property
    def total(self) -> int:
        return self.inserted + self.updated


class ReviewStore(abc.ABC):
    """Backend-agnostic storage interface for normalized reviews."""

    @abc.abstractmethod
    def upsert_many(self, reviews: Iterable[Dict[str, Any]]) -> UpsertResult:
        """Insert new reviews and refresh existing ones (idempotent)."""

    @abc.abstractmethod
    def fetch_all(self) -> List[Dict[str, Any]]:
        """Return all stored reviews as normalized dicts."""

    @abc.abstractmethod
    def close(self) -> None:
        ...

    def __enter__(self):
        return self

    def __exit__(self, exc_type, exc, tb):
        self.close()


class SQLiteReviewStore(ReviewStore):
    """SQLite-backed store with a unique index for deduplication.

    A ``dedup_key`` column carries the deduplication identity from
    :func:`normalizer.compute_dedup_key`; a UNIQUE index on it makes repeated
    runs idempotent via ``INSERT ... ON CONFLICT``.
    """

    def __init__(self, path: str):
        self.path = path
        self.conn = sqlite3.connect(path)
        self.conn.row_factory = sqlite3.Row
        self._create_schema()

    def _create_schema(self) -> None:
        # Generate the column list from the single schema definition. rating is
        # given INTEGER affinity; everything else is TEXT.
        column_defs = []
        for name in REVIEW_FIELDS:
            affinity = "INTEGER" if name == "rating" else "TEXT"
            column_defs.append(f"{name} {affinity}")
        columns = ",\n                ".join(column_defs)
        self.conn.executescript(
            f"""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedup_key TEXT NOT NULL,
                {columns},
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            );
            CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_dedup_key
                ON reviews (dedup_key);
            """
        )
        self.conn.commit()

    def upsert_many(self, reviews: Iterable[Dict[str, Any]]) -> UpsertResult:
        now = utc_now_iso()
        inserted = 0
        updated = 0
        cur = self.conn.cursor()

        placeholders = ", ".join(["?"] * (len(REVIEW_FIELDS) + 3))
        col_list = ", ".join(["dedup_key", *REVIEW_FIELDS, "created_at", "updated_at"])
        # On conflict, refresh mutable fields (e.g. a newly added developer
        # response) and bump updated_at, while preserving the original
        # created_at.
        update_assignments = ", ".join(
            f"{name}=excluded.{name}" for name in REVIEW_FIELDS
        )
        sql = (
            f"INSERT INTO reviews ({col_list}) VALUES ({placeholders}) "
            f"ON CONFLICT(dedup_key) DO UPDATE SET {update_assignments}, "
            f"updated_at=excluded.updated_at"
        )

        for review in reviews:
            dedup_key = compute_dedup_key(review)
            exists = cur.execute(
                "SELECT 1 FROM reviews WHERE dedup_key = ?", (dedup_key,)
            ).fetchone()
            values = [dedup_key]
            values.extend(review.get(field) for field in REVIEW_FIELDS)
            values.extend([now, now])
            cur.execute(sql, values)
            if exists:
                updated += 1
            else:
                inserted += 1

        self.conn.commit()
        return UpsertResult(inserted=inserted, updated=updated)

    def fetch_all(self) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            f"SELECT {', '.join(REVIEW_FIELDS)} FROM reviews ORDER BY id"
        )
        return [dict(row) for row in cur.fetchall()]

    def close(self) -> None:
        self.conn.close()

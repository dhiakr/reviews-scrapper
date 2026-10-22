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

from normalizer import REVIEW_FIELDS, compute_dedup_key, merge_reviews, utc_now_iso


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
        self.conn = sqlite3.connect(path, timeout=30)
        self.conn.row_factory = sqlite3.Row
        self.conn.execute("PRAGMA busy_timeout = 30000")
        self.conn.execute("PRAGMA journal_mode = WAL")
        self._create_schema()

    def _create_schema(self) -> None:
        # Generate the column list from the single schema definition. rating is
        # given INTEGER affinity; everything else is TEXT.
        column_defs = []
        for name in REVIEW_FIELDS:
            affinity = "INTEGER" if name == "rating" else "TEXT"
            column_defs.append(f"{name} {affinity}")
        columns = ",\n                ".join(column_defs)
        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            previous_fields = {
                row["name"] for row in self.conn.execute("PRAGMA table_info(reviews)")
            }
            self.conn.execute(f"""
            CREATE TABLE IF NOT EXISTS reviews (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedup_key TEXT NOT NULL,
                {columns},
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
            """)
            for name in REVIEW_FIELDS:
                if previous_fields and name not in previous_fields:
                    affinity = "INTEGER" if name == "rating" else "TEXT"
                    self.conn.execute(f"ALTER TABLE reviews ADD COLUMN {name} {affinity}")
            self.conn.execute(
                "CREATE TABLE IF NOT EXISTS review_scraper_schema_versions "
                "(component TEXT PRIMARY KEY, version INTEGER NOT NULL)"
            )
            version = self.conn.execute(
                "SELECT version FROM review_scraper_schema_versions WHERE component = 'reviews'"
            ).fetchone()
            if not version or version["version"] < 2:
                self._migrate_identity(
                    legacy_google_provenance=bool(previous_fields) and "query_language" not in previous_fields
                )
                self.conn.execute(
                    "INSERT OR REPLACE INTO review_scraper_schema_versions (component, version) "
                    "VALUES ('reviews', 2)"
                )
            self.conn.execute(
                "CREATE UNIQUE INDEX IF NOT EXISTS idx_reviews_dedup_key ON reviews (dedup_key)"
            )

    def _migrate_identity(self, *, legacy_google_provenance: bool) -> None:
        """Rebuild identities transactionally; old country-specific keys may collide.

        Work in a separate table so a failed migration leaves the old data
        intact. Process one row at a time to avoid loading the archive in RAM.
        Older Google rows contain inferred countries; their true queried
        storefront is unavailable and must be marked unknown.
        """
        columns = ", ".join(
            f"{name} {'INTEGER' if name == 'rating' else 'TEXT'}" for name in REVIEW_FIELDS
        )
        self.conn.execute(f"""
            CREATE TABLE reviews_identity_migration (
                id INTEGER PRIMARY KEY AUTOINCREMENT,
                dedup_key TEXT NOT NULL UNIQUE,
                {columns},
                created_at TEXT NOT NULL,
                updated_at TEXT NOT NULL
            )
        """)
        fieldnames = ["id", "dedup_key", *REVIEW_FIELDS, "created_at", "updated_at"]
        sql = (
            f"INSERT INTO reviews_identity_migration ({', '.join(fieldnames)}) "
            f"VALUES ({', '.join('?' for _ in fieldnames)})"
        )
        for row in self.conn.execute("SELECT * FROM reviews ORDER BY id"):
            review = dict(row)
            if legacy_google_provenance and review.get("platform") == "google_play":
                review["country"] = None
                review["reviewer_country"] = None
            key = compute_dedup_key(review)
            existing = self.conn.execute(
                "SELECT * FROM reviews_identity_migration WHERE dedup_key = ?", (key,)
            ).fetchone()
            if existing:
                merged = merge_reviews(dict(existing), review)
                merged["id"] = existing["id"]
                merged["created_at"] = min(existing["created_at"], review["created_at"])
                merged["updated_at"] = max(existing["updated_at"], review["updated_at"])
                self.conn.execute(
                    "DELETE FROM reviews_identity_migration WHERE dedup_key = ?", (key,)
                )
                review = merged
            review["dedup_key"] = key
            self.conn.execute(sql, [review.get(name) for name in fieldnames])
        self.conn.execute("DROP TABLE reviews")
        self.conn.execute("ALTER TABLE reviews_identity_migration RENAME TO reviews")

    def upsert_many(self, reviews: Iterable[Dict[str, Any]]) -> UpsertResult:
        now = utc_now_iso()
        inserted = 0
        updated = 0
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

        with self.conn:
            self.conn.execute("BEGIN IMMEDIATE")
            for review in reviews:
                dedup_key = compute_dedup_key(review)
                exists = self.conn.execute(
                    f"SELECT {', '.join(REVIEW_FIELDS)} FROM reviews WHERE dedup_key = ?", (dedup_key,)
                ).fetchone()
                if exists:
                    review = merge_reviews(dict(exists), review)
                values = [dedup_key]
                values.extend(review.get(field) for field in REVIEW_FIELDS)
                values.extend([now, now])
                self.conn.execute(sql, values)
                if exists:
                    updated += 1
                else:
                    inserted += 1
        return UpsertResult(inserted=inserted, updated=updated)

    def fetch_all(self) -> List[Dict[str, Any]]:
        cur = self.conn.execute(
            f"SELECT {', '.join(REVIEW_FIELDS)} FROM reviews ORDER BY id"
        )
        return [dict(row) for row in cur.fetchall()]

    def close(self) -> None:
        self.conn.close()

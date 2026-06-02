"""Normalization and deduplication helpers.

Both store adapters convert their raw payloads into the single shared schema
defined here, so the rest of the pipeline only deals with one shape of data.
"""

from __future__ import annotations

import hashlib
from datetime import datetime, timezone
from typing import Any, Dict, Optional

# Canonical field order for the normalized review schema. Exporters and the
# SQLite layer both rely on this list so the schema stays in one place.
REVIEW_FIELDS = [
    "platform",
    "source_url",
    "app_id",
    "country",
    "language",
    "review_id",
    "rating",
    "title",
    "text",
    "reviewer_name",
    "review_date",
    "app_version",
    "developer_response",
    "developer_response_date",
    "scraped_at",
]


# Best-effort map from a detected language to a representative country code.
# Used to infer a Google Play review's country, since the public review data
# does not include the reviewer's country (only the storefront we queried).
LANG_TO_COUNTRY = {
    "en": "us", "pt": "pt", "es": "es", "fr": "fr", "de": "de", "it": "it",
    "nl": "nl", "pl": "pl", "ru": "ru", "tr": "tr", "ja": "jp", "ko": "kr",
    "zh-cn": "cn", "zh-tw": "tw", "ar": "sa", "hi": "in", "id": "id",
    "th": "th", "vi": "vn", "sv": "se", "da": "dk", "no": "no", "fi": "fi",
    "cs": "cz", "el": "gr", "he": "il", "uk": "ua", "ro": "ro", "hu": "hu",
    "bg": "bg", "hr": "hr", "sk": "sk", "ca": "es", "fa": "ir", "ms": "my",
    "tl": "ph", "lt": "lt", "lv": "lv", "et": "ee", "sl": "si", "sr": "rs",
}

_LANGDETECT_READY = False


def utc_now_iso() -> str:
    """Current UTC time as an ISO-8601 string (seconds precision)."""
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


def detect_language(text: Optional[str]) -> Optional[str]:
    """Detect the language code of a review's text.

    Returns a lowercase code (e.g. ``pt``, ``en``, ``zh-cn``) or ``None`` when
    detection isn't possible (empty/too-short text, or langdetect unavailable).
    Deterministic: langdetect's RNG seed is pinned so repeated runs agree.
    """
    if not text or len(text.strip()) < 3:
        return None
    global _LANGDETECT_READY
    try:
        if not _LANGDETECT_READY:
            from langdetect import DetectorFactory

            DetectorFactory.seed = 0
            _LANGDETECT_READY = True
        from langdetect import detect

        return detect(text).lower()
    except Exception:  # noqa: BLE001 - any detection failure -> unknown
        return None


def infer_country(language: Optional[str]) -> Optional[str]:
    """Best-effort country code for a detected language, or ``None``."""
    if not language:
        return None
    return LANG_TO_COUNTRY.get(language.lower())


def to_iso(value: Any) -> Optional[str]:
    """Best-effort conversion of a date-ish value to an ISO-8601 string."""
    if value is None or value == "":
        return None
    if isinstance(value, datetime):
        return value.isoformat()
    # Already a string (or something stringifiable) — leave as-is.
    return str(value)


def _clean_int(value: Any) -> Optional[int]:
    if value is None or value == "":
        return None
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def make_review(
    *,
    platform: str,
    source_url: str,
    app_id: str,
    country: Optional[str],
    language: Optional[str] = None,
    review_id: Optional[str] = None,
    rating: Any = None,
    title: Optional[str] = None,
    text: Optional[str] = None,
    reviewer_name: Optional[str] = None,
    review_date: Any = None,
    app_version: Optional[str] = None,
    developer_response: Optional[str] = None,
    developer_response_date: Any = None,
    scraped_at: Optional[str] = None,
) -> Dict[str, Any]:
    """Build a normalized review dict with every schema field present."""
    return {
        "platform": platform,
        "source_url": source_url,
        "app_id": app_id,
        "country": country,
        "language": language,
        "review_id": str(review_id) if review_id is not None else None,
        "rating": _clean_int(rating),
        "title": title or None,
        "text": text or None,
        "reviewer_name": reviewer_name or None,
        "review_date": to_iso(review_date),
        "app_version": app_version or None,
        "developer_response": developer_response or None,
        "developer_response_date": to_iso(developer_response_date),
        "scraped_at": scraped_at or utc_now_iso(),
    }


def compute_dedup_key(review: Dict[str, Any]) -> str:
    """Return a stable deduplication key for a normalized review.

    Rules:
    * Prefer ``platform + app_id + country + review_id`` when a review_id exists.
    * Otherwise hash ``platform + app_id + country + rating + title + text +
      review_date`` so repeated runs stay idempotent.
    """
    platform = review.get("platform") or ""
    app_id = review.get("app_id") or ""
    country = review.get("country") or ""
    review_id = review.get("review_id")

    if review_id:
        return f"{platform}|{app_id}|{country}|{review_id}"

    parts = [
        platform,
        app_id,
        country,
        str(review.get("rating") if review.get("rating") is not None else ""),
        review.get("title") or "",
        review.get("text") or "",
        review.get("review_date") or "",
    ]
    digest = hashlib.sha256("|".join(parts).encode("utf-8")).hexdigest()
    return f"{platform}|{app_id}|{country}|h:{digest}"


def deduplicate(reviews) -> list:
    """Remove duplicate reviews (by dedup key), preserving first-seen order."""
    seen = set()
    out = []
    for review in reviews:
        key = compute_dedup_key(review)
        if key in seen:
            continue
        seen.add(key)
        out.append(review)
    return out

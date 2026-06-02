"""URL detection and app-identifier extraction.

Given a raw URL provided by the user, decide whether it points at Google Play
or the Apple App Store, and pull out the identifier each store needs.

Deterministic, no network access.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Optional
from urllib.parse import parse_qs, urlparse

PLATFORM_GOOGLE_PLAY = "google_play"
PLATFORM_APP_STORE = "app_store"

# Apple app id looks like ".../id123456789" possibly followed by a query string.
_APPLE_ID_RE = re.compile(r"/id(\d+)")
# Apple country code is the first path segment, e.g. /us/app/...
_APPLE_COUNTRY_RE = re.compile(r"^/([a-z]{2})/app/", re.IGNORECASE)


class UrlParseError(ValueError):
    """Raised when a URL is unsupported or is missing a required identifier."""


@dataclass
class ParsedUrl:
    """Result of parsing a store URL."""

    platform: str
    app_id: str
    source_url: str
    # Country embedded in the URL when present (Apple only). May be None.
    country: Optional[str] = None


def detect_platform(url: str) -> str:
    """Return the platform constant for a URL, or raise UrlParseError."""
    host = (urlparse(url).hostname or "").lower()
    if host.endswith("play.google.com"):
        return PLATFORM_GOOGLE_PLAY
    if host.endswith("apps.apple.com") or host.endswith("itunes.apple.com"):
        return PLATFORM_APP_STORE
    raise UrlParseError(
        f"Unsupported URL host {host!r}. Expected a play.google.com or "
        f"apps.apple.com URL."
    )


def _extract_google_play_id(url: str) -> str:
    """Extract the package name from the Play Store `id` query parameter."""
    query = parse_qs(urlparse(url).query)
    ids = query.get("id")
    if not ids or not ids[0].strip():
        raise UrlParseError(
            "Google Play URL is missing the `id` query parameter "
            "(expected e.g. ?id=com.company.app)."
        )
    return ids[0].strip()


def _extract_app_store_id(url: str) -> str:
    """Extract the numeric app id from an Apple App Store URL path."""
    match = _APPLE_ID_RE.search(urlparse(url).path)
    if not match:
        raise UrlParseError(
            "Apple App Store URL is missing the numeric app id "
            "(expected a path segment like /id123456789)."
        )
    return match.group(1)


def _extract_app_store_country(url: str) -> Optional[str]:
    """Extract the storefront country from an Apple URL path, if present."""
    match = _APPLE_COUNTRY_RE.match(urlparse(url).path)
    if match:
        return match.group(1).lower()
    return None


def parse_url(url: str) -> ParsedUrl:
    """Detect the platform and extract the app id (and country for Apple).

    Raises UrlParseError for unsupported URLs or missing identifiers.
    """
    if not url or not url.strip():
        raise UrlParseError("Empty URL provided.")

    url = url.strip()
    platform = detect_platform(url)

    if platform == PLATFORM_GOOGLE_PLAY:
        return ParsedUrl(
            platform=platform,
            app_id=_extract_google_play_id(url),
            source_url=url,
            country=None,
        )

    # Apple App Store
    return ParsedUrl(
        platform=platform,
        app_id=_extract_app_store_id(url),
        source_url=url,
        country=_extract_app_store_country(url),
    )

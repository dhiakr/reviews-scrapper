"""Command-line entry point for the review scraper.

Subcommands
-----------
scrape   Detect the store from a URL, collect retrievable public reviews for
         the requested countries/languages, deduplicate, and export and/or
         store them.
group    Post-process already-collected reviews (CSV/JSON/SQLite) and add AI
         grouping fields. Does not scrape.

Run from inside the ``review_scraper`` directory:

    python main.py scrape "<url>" --countries us gb --lang en --out reviews.csv
    python main.py group --input reviews.csv --out grouped_reviews.csv
"""

from __future__ import annotations

import argparse
import logging
import sys
from typing import Any, Dict, List

from ai_grouping.classifier import classify_reviews
from exporters import export_reviews, load_reviews
from normalizer import deduplicate
from storage import SQLiteReviewStore
from stores import app_store, google_play
from url_parser import (
    PLATFORM_APP_STORE,
    PLATFORM_GOOGLE_PLAY,
    UrlParseError,
    parse_url,
)

logger = logging.getLogger("review_scraper")


def _scrape_per_country(parsed, args) -> List[Dict[str, Any]]:
    """Scrape each country/language, isolating failures so the run continues."""
    collected: List[Dict[str, Any]] = []
    failures: List[str] = []

    if parsed.platform == PLATFORM_GOOGLE_PLAY:
        languages = args.lang or ["en"]
        for country in args.countries:
            for language in languages:
                label = f"country={country} lang={language}"
                try:
                    reviews = google_play.scrape(
                        app_id=parsed.app_id,
                        source_url=parsed.source_url,
                        country=country,
                        language=language,
                        max_reviews=args.max_reviews,
                        newest_first=not args.most_relevant,
                    )
                    collected.extend(reviews)
                    logger.info("OK Google Play %s: %d reviews", label, len(reviews))
                except Exception as exc:  # noqa: BLE001 - isolate per-country failures
                    failures.append(label)
                    logger.error("FAILED Google Play %s: %s", label, exc)

    elif parsed.platform == PLATFORM_APP_STORE:
        # Use CLI countries; if none given, fall back to the URL's country.
        countries = args.countries or ([parsed.country] if parsed.country else [])
        if not countries:
            countries = ["us"]
            logger.warning("No country given and none in URL; defaulting to 'us'.")
        for country in countries:
            label = f"country={country}"
            try:
                reviews = app_store.scrape(
                    app_id=parsed.app_id,
                    source_url=parsed.source_url,
                    country=country,
                    max_reviews=args.max_reviews,
                )
                collected.extend(reviews)
                logger.info("OK App Store %s: %d reviews", label, len(reviews))
            except Exception as exc:  # noqa: BLE001 - isolate per-country failures
                failures.append(label)
                logger.error("FAILED App Store %s: %s", label, exc)

    if failures:
        logger.warning(
            "%d of the requested targets failed: %s. Continuing with partial "
            "results.",
            len(failures),
            ", ".join(failures),
        )
    return collected


def cmd_scrape(args) -> int:
    try:
        parsed = parse_url(args.url)
    except UrlParseError as exc:
        logger.error("Could not parse URL: %s", exc)
        return 2

    logger.info(
        "Detected platform=%s app_id=%s", parsed.platform, parsed.app_id
    )

    reviews = _scrape_per_country(parsed, args)
    reviews = deduplicate(reviews)
    logger.info("Collected %d unique reviews after deduplication.", len(reviews))

    if not reviews:
        logger.warning("No reviews collected. Nothing to export or store.")

    # Store in SQLite if requested (also deduplicates across past runs).
    if args.db:
        with SQLiteReviewStore(args.db) as store:
            result = store.upsert_many(reviews)
        logger.info(
            "SQLite %s: %d new, %d updated (idempotent).",
            args.db,
            result.inserted,
            result.updated,
        )

    # Export to a file if requested.
    if args.out:
        written = export_reviews(reviews, args.out)
        logger.info("Exported %d reviews to %s", written, args.out)

    if not args.db and not args.out:
        logger.warning(
            "Neither --out nor --db given; results were collected but not "
            "persisted. Use --out FILE and/or --db FILE."
        )

    return 0


def cmd_group(args) -> int:
    reviews = load_reviews(args.input)
    logger.info("Loaded %d reviews from %s", len(reviews), args.input)
    grouped = classify_reviews(reviews, backend=args.backend)
    written = export_reviews(grouped, args.out)
    logger.info("Wrote %d grouped reviews to %s", written, args.out)
    return 0


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="review_scraper",
        description="Collect retrievable public app reviews from Google Play "
        "and the Apple App Store.",
    )
    parser.add_argument(
        "-v", "--verbose", action="store_true", help="Enable debug logging."
    )
    sub = parser.add_subparsers(dest="command", required=True)

    p_scrape = sub.add_parser("scrape", help="Scrape reviews from a store URL.")
    p_scrape.add_argument("url", help="Google Play or Apple App Store URL.")
    p_scrape.add_argument(
        "--countries",
        nargs="+",
        default=[],
        metavar="CC",
        help="Country codes to scrape (e.g. us gb pt).",
    )
    p_scrape.add_argument(
        "--lang",
        nargs="+",
        default=None,
        metavar="LANG",
        help="Language codes (Google Play only; e.g. en pt). Default: en.",
    )
    p_scrape.add_argument(
        "--max-reviews",
        type=int,
        default=None,
        dest="max_reviews",
        help="Cap reviews fetched per country/language (default: all retrievable).",
    )
    p_scrape.add_argument(
        "--most-relevant",
        action="store_true",
        help="Fetch most-relevant first instead of newest first (Google Play).",
    )
    p_scrape.add_argument("--out", help="Export path (.csv or .json).")
    p_scrape.add_argument("--db", help="SQLite database path to store reviews.")
    p_scrape.set_defaults(func=cmd_scrape)

    p_group = sub.add_parser(
        "group", help="AI-group already-collected reviews (no scraping)."
    )
    p_group.add_argument(
        "--input", required=True, help="Input reviews file (.csv or .json)."
    )
    p_group.add_argument(
        "--out", required=True, help="Output path for grouped reviews (.csv or .json)."
    )
    p_group.add_argument(
        "--backend",
        default="rule_based",
        choices=["rule_based", "llm"],
        help="Classification backend (default: rule_based, deterministic).",
    )
    p_group.set_defaults(func=cmd_group)

    return parser


def main(argv=None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(message)s",
    )
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

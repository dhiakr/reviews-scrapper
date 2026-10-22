"""CLI collection, scheduled archiving, and separate review grouping."""

from __future__ import annotations

import argparse
import json
import logging
import os
import sys
import time
from contextlib import nullcontext
from pathlib import Path

from ai_grouping.classifier import classify_reviews
from exporters import export_reviews, load_reviews
from jobs import SQLiteJobStore
from runner import build_targets, run_job
from storage import SQLiteReviewStore
from stores.app_store import ALL_COUNTRIES
from url_parser import PLATFORM_GOOGLE_PLAY, UrlParseError, parse_url

logger = logging.getLogger("review_scraper")


def _expand_countries(countries):
    if countries and any(code.lower() == "all" for code in countries):
        return list(ALL_COUNTRIES)
    return list(dict.fromkeys(code.lower() for code in (countries or [])))


def _positive(value):
    number = int(value)
    if number <= 0:
        raise argparse.ArgumentTypeError("must be positive")
    return number


def _nonnegative(value):
    number = int(value)
    if number < 0:
        raise argparse.ArgumentTypeError("must not be negative")
    return number


def _job_path():
    directory = Path(os.environ.get("REVIEW_SCRAPER_DATA_DIR",
                                   str(Path(__file__).resolve().parents[1] / ".review_scraper")))
    return str(directory / "jobs.sqlite3")


def cmd_scrape(args):
    try:
        parsed = parse_url(args.url)
    except UrlParseError as exc:
        logger.error("Could not parse URL: %s", exc)
        return 2
    path = Path(args.job_db)
    path.parent.mkdir(parents=True, exist_ok=True)
    with SQLiteJobStore(str(path)) as store:
        if args.resume:
            try:
                job = store.get_job(args.resume)
            except KeyError:
                logger.error("Saved run not found in %s", path)
                return 2
            config = job["config"]
            if config["app_id"] != parsed.app_id or config["platform"] != parsed.platform:
                logger.error("Recovery code belongs to a different app")
                return 2
            job_id = job["id"]
            logger.info("Resuming original countries, languages and limits from saved run")
        else:
            countries = _expand_countries(args.countries) or [parsed.country or "us"]
            languages = list(dict.fromkeys(code.lower() for code in (args.lang or ["en"])))
            config = dict(platform=parsed.platform, app_id=parsed.app_id,
                          source_url=parsed.source_url, countries=countries,
                          languages=languages if parsed.platform == PLATFORM_GOOGLE_PLAY else [],
                          max_reviews=args.max_reviews, newest_first=not args.most_relevant,
                          page_delay_ms=args.page_delay_ms, max_retries=args.retries)
            job_id = store.create_job(config, build_targets(parsed.platform, countries, languages))
    logger.info("Saved run: %s (checkpoints: %s)", job_id, path)
    archive = SQLiteReviewStore(args.db) if args.db else nullcontext(None)
    try:
        with archive as store:
            job = run_job(str(path), job_id, on_page=store.upsert_many if store else None)
    except KeyboardInterrupt:
        logger.warning("Interrupted. Completed pages saved; resume with --resume %s", job_id)
        return 130
    with SQLiteJobStore(str(path)) as store:
        # When resuming with a newly supplied --db, include earlier saved pages.
        if args.db or args.out:
            reviews = store.fetch_reviews(job_id)
            if args.db:
                with SQLiteReviewStore(args.db) as archive:
                    archive.upsert_many(reviews)
            if args.out:
                logger.info("Exported %d unique reviews to %s",
                            export_reviews(reviews, args.out), args.out)
    logger.info("%s: %d unique reviews, date range %s to %s", job["status"],
                job["review_count"], job["oldest_review_date"], job["newest_review_date"])
    for target in job["targets"]:
        logger.info("%s: %s, %d saved, stop=%s%s", target["id"], target["status"],
                    target["review_count"], target["stop_reason"],
                    f", error={target['error']}" if target.get("error") else "")
    logger.warning("Collected public-feed coverage only; historical completeness is unknown.")
    if args.report:
        Path(args.report).write_text(json.dumps(job, ensure_ascii=False, indent=2), encoding="utf-8")
    return 0 if job["status"] == "finished" else 1


def cmd_watch(args):
    """Repeat fresh collection runs; page upserts maintain the archive."""
    cycle = 0
    last_code = 0
    try:
        while args.cycles is None or cycle < args.cycles:
            cycle += 1
            logger.info("Scheduled collection cycle %d", cycle)
            last_code = cmd_scrape(args)
            if last_code in {2, 130}:
                return last_code
            if args.cycles is None or cycle < args.cycles:
                time.sleep(args.interval)
    except KeyboardInterrupt:
        logger.info("Scheduled collection stopped")
        return 130
    return last_code


def cmd_group(args):
    reviews = load_reviews(args.input)
    grouped = classify_reviews(reviews, backend=args.backend)
    logger.info("Wrote %d grouped reviews to %s", export_reviews(grouped, args.out), args.out)
    return 0


def build_parser():
    parser = argparse.ArgumentParser(prog="review_scraper",
                                     description="Collect retrievable public app reviews with saved progress.")
    parser.add_argument("-v", "--verbose", action="store_true", help="Debug logging")
    sub = parser.add_subparsers(dest="command", required=True)
    for command in ("scrape", "watch"):
        scrape = sub.add_parser(command, help="Collect reviews" if command == "scrape"
                               else "Repeat collection on an always-on host")
        scrape.add_argument("url", help="Google Play or Apple App Store URL")
        scrape.add_argument("--countries", nargs="+", default=[], metavar="CC",
                            help="Storefront codes; 'all' uses the configured country list")
        scrape.add_argument("--lang", nargs="+", default=None, metavar="LANG",
                            help="Google query languages, e.g. en pt es (default: en)")
        scrape.add_argument("--max-reviews", type=_positive, default=None,
                            help="Cap per country/language; omitted = all retrievable")
        scrape.add_argument("--most-relevant", action="store_true")
        scrape.add_argument("--page-delay-ms", type=_nonnegative, default=600)
        scrape.add_argument("--retries", type=_nonnegative, default=5)
        scrape.add_argument("--out", help="Export .csv, .json or .xlsx")
        scrape.add_argument("--db", required=command == "watch", help="Persistent SQLite review archive")
        scrape.add_argument("--job-db", default=_job_path(), help="SQLite saved runs and checkpoints")
        scrape.add_argument("--report", help="Write a JSON coverage report")
        if command == "scrape":
            scrape.add_argument("--resume", help="Recovery code; uses the saved run's original options")
            scrape.set_defaults(func=cmd_scrape)
        else:
            scrape.add_argument("--interval", type=_positive, default=3600,
                                help="Seconds between completed runs (default: 3600)")
            scrape.add_argument("--cycles", type=_positive, default=None,
                                help="Stop after this many runs; omitted = run until stopped")
            scrape.set_defaults(func=cmd_watch, resume=None)
    group = sub.add_parser("group", help="Classify already collected reviews")
    group.add_argument("--input", required=True)
    group.add_argument("--out", required=True)
    group.add_argument("--backend", default="rule_based", choices=["rule_based", "llm"])
    group.set_defaults(func=cmd_group)
    return parser


def main(argv=None):
    args = build_parser().parse_args(argv)
    logging.basicConfig(level=logging.DEBUG if args.verbose else logging.INFO,
                        format="%(levelname)s %(message)s")
    return args.func(args)


if __name__ == "__main__":
    sys.exit(main())

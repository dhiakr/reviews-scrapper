"""Streamlit UI for explicit review coverage and checkpointed collection jobs."""

from __future__ import annotations

import json
import os
import re
import sqlite3
import sys
import uuid
from pathlib import Path

import streamlit as st

PACKAGE_DIR = str(Path(__file__).resolve().parent / "review_scraper")
if PACKAGE_DIR not in sys.path:
    sys.path.insert(0, PACKAGE_DIR)

from exporters import reviews_to_bytes  # noqa: E402
from jobs import SQLiteJobStore  # noqa: E402
from runner import JobManager, build_targets  # noqa: E402
from stores.app_store import ALL_COUNTRIES  # noqa: E402
from url_parser import PLATFORM_GOOGLE_PLAY, UrlParseError, parse_url  # noqa: E402


def parse_codes(raw):
    return list(dict.fromkeys(raw.lower().replace(",", " ").split()))


def valid_codes(codes, language=False):
    pattern = r"[a-z]{2,3}(?:-[a-z0-9]{2,8})*" if language else r"[a-z]{2}"
    return bool(codes) and all(re.fullmatch(pattern, code) for code in codes)


@st.cache_resource
def get_manager(path):
    return JobManager(path)


st.set_page_config(page_title="App Review Scraper", page_icon="📲", layout="centered")
st.markdown("""
<style>
.block-container {max-width: 880px; padding-top: 3rem;}
h1 {letter-spacing: -0.02em;}
div[data-testid="stButton"] > button {border-radius: 999px;}
</style>
""", unsafe_allow_html=True)
st.title("App Review Scraper")
st.write("Collect retrievable public reviews from Google Play or the Apple App Store.")
st.caption("Public feeds cannot guarantee every historical review. Apple exposes up to "
           "500 recent reviews per storefront; Google Play availability varies by query.")

data_dir = Path(os.environ.get("REVIEW_SCRAPER_DATA_DIR",
                             str(Path(__file__).resolve().parent / ".review_scraper")))
try:
    data_dir.mkdir(parents=True, exist_ok=True)
    db_path = str(data_dir / "jobs.sqlite3")
    with SQLiteJobStore(db_path):
        pass
except (OSError, sqlite3.Error) as exc:
    st.error(f"Saved runs are unavailable: {exc}")
    st.stop()
manager = get_manager(db_path)
st.session_state.setdefault("job_ids", [])

url = st.text_input("Store URL", key="url_input",
                   placeholder="https://play.google.com/store/apps/details?id=com.example.app")
parsed = None
if url.strip():
    try:
        parsed = parse_url(url.strip())
    except UrlParseError as exc:
        st.error(f"Could not parse URL: {exc}")

if parsed:
    is_google = parsed.platform == PLATFORM_GOOGLE_PLAY
    cols = st.columns(2)
    cols[0].metric("Platform", "Google Play" if is_google else "Apple App Store")
    cols[1].metric("App ID", parsed.app_id)
    sweep_all = st.checkbox(f"Query all {len(ALL_COUNTRIES)} configured storefronts (slower)",
                            value=not is_google, key=f"sweep_{parsed.platform}")
    countries_raw = st.text_input(
        "Storefront countries", value=parsed.country or "us", disabled=sweep_all,
        key=f"countries_{parsed.platform}",
        help="Comma or space separated country codes, for example us gb pt. "
             "These are the storefronts queried, not verified reviewer locations.",
    )
    languages_raw = ""
    if is_google:
        languages_raw = st.text_input(
            "Review languages to query", value="en", key="query_languages",
            help="Comma or space separated codes, for example en pt es fr de. "
                 "Each selected language is queried in every selected country. "
                 "Language detection afterward does not expand this coverage.",
        )
        st.caption("The export keeps the queried language and detected review language separately. "
                   "Google does not provide a verified reviewer country.")
    else:
        st.caption("Apple reviews are queried per storefront; text language is detected afterward. "
                   "The configured country list is not a guarantee of every available storefront.")
    newest_first = st.checkbox("Newest reviews first", value=True) if is_google else True
    with st.expander("Collection settings"):
        limit = st.number_input("Maximum reviews per country/language (0 = all retrievable)",
                                min_value=0, value=0, step=100)
        page_delay_ms = st.slider("Delay between pages (ms)", 0, 3000, 600, step=100)
        max_retries = st.slider("Retries per page", 0, 8, 5)
        st.caption("Slower requests can reduce transient failures. They cannot remove feed limits.")
    active = any(manager.is_running(job_id) for job_id in st.session_state.job_ids)
    if st.button("Scrape reviews", type="primary", disabled=active):
        countries = list(ALL_COUNTRIES) if sweep_all else parse_codes(countries_raw)
        languages = parse_codes(languages_raw) if is_google else []
        if not valid_codes(countries):
            st.error("Enter two-letter country codes, for example us gb pt.")
        elif is_google and not valid_codes(languages, language=True):
            st.error("Enter review language codes, for example en pt es.")
        else:
            config = dict(platform=parsed.platform, app_id=parsed.app_id,
                          source_url=parsed.source_url, countries=countries,
                          languages=languages, max_reviews=int(limit) or None,
                          newest_first=newest_first, page_delay_ms=page_delay_ms,
                          max_retries=max_retries)
            with SQLiteJobStore(db_path) as store:
                job_id = store.create_job(config, build_targets(parsed.platform, countries, languages))
            st.session_state.job_ids.append(job_id)
            st.session_state.selected_job = job_id
            manager.start(job_id)
            st.rerun()
    if active:
        st.caption("A collection is already running. Its progress and saved reviews appear below.")

with st.expander("Restore a saved run"):
    recovery_code = st.text_input("Recovery code", help="Use the code shown with a previous run.")
    if st.button("Restore run"):
        try:
            uuid.UUID(recovery_code.strip())
            with SQLiteJobStore(db_path) as store:
                recovered = store.get_job(recovery_code.strip())
            if not recovered:
                raise KeyError(recovery_code)
        except (ValueError, KeyError):
            st.error("Saved run not found. Check the code and use the same deployment.")
        else:
            if recovered["id"] not in st.session_state.job_ids:
                st.session_state.job_ids.append(recovered["id"])
            st.session_state.selected_job = recovered["id"]
            st.rerun()


@st.fragment(run_every="2s")
def show_job(job_id):
    try:
        with SQLiteJobStore(db_path) as store:
            job = store.get_job(job_id)
            preview = store.fetch_reviews(job_id, limit=200)
    except KeyError:
        st.warning("This run is no longer on this server. Restore a database backup if available.")
        return
    running = manager.is_running(job_id)
    status = job["status"]
    if status in {"running", "queued"} and not running:
        status = "interrupted"
    done = sum(target["status"] not in {"queued", "pending", "running"}
               for target in job["targets"])
    st.divider()
    st.subheader(f"Saved run · {status}")
    st.caption(job["config"]["source_url"])
    st.progress(done / max(len(job["targets"]), 1),
                text=f"{done} of {len(job['targets'])} country/language queries stopped")
    st.metric("Unique reviews saved", job.get("review_count", 0))
    st.write(f"Collected date range: {job.get('oldest_review_date') or 'unknown'} "
             f"to {job.get('newest_review_date') or 'unknown'}")
    st.caption("A stopped query does not prove historical completeness. "
               "Empty feeds can also result from temporary store behavior.")
    if error := manager.error(job_id):
        st.error(f"Worker stopped: {error}. Saved pages are available below.")
    coverage = []
    for target in job["targets"]:
        coverage.append({
            "country": target["country"], "query_language": target["language"],
            "status": target["status"], "reviews": target.get("review_count", 0),
            "pages": target.get("pages_fetched", 0),
            "stop_reason": (target.get("stop_reason") or "").replace("_", " "),
            "error": target.get("error") or "",
        })
    st.dataframe(coverage, hide_index=True, use_container_width=True)
    warnings = dict.fromkeys(warning for target in job["targets"]
                             for warning in (target.get("warnings") or []))
    for warning in warnings:
        st.warning(warning)
    if running:
        if st.button("Stop after the current request", key=f"stop_{job_id}"):
            manager.cancel(job_id)
            st.info("Stop requested. Completed pages will remain saved.")
    elif status in {"partial", "failed", "cancelled", "interrupted"}:
        if st.button("Resume unfinished queries", key=f"resume_{job_id}"):
            manager.start(job_id)
            st.rerun(scope="fragment")
        st.caption("If Google rejects an expired cursor, start a fresh run. "
                   "The saved reviews from this run remain downloadable.")
    st.write("Recovery code (keep private):")
    st.code(job_id, language=None)
    st.caption("Saved pages survive browser refreshes and worker interruptions. "
               "Streamlit Community Cloud may reset local files during redeploys; "
               "download results before then. Collection pauses when the host stops.")
    if preview:
        st.dataframe(preview, hide_index=True, use_container_width=True)
        st.caption(f"Showing up to 200 of {job.get('review_count', 0)} saved reviews.")
        fmt = st.selectbox("Download format", ["csv", "json", "xlsx"], key=f"format_{job_id}")
        if st.button("Prepare review download", key=f"prepare_{job_id}"):
            with SQLiteJobStore(db_path) as store:
                reviews = store.fetch_reviews(job_id)
            with st.spinner("Preparing saved reviews…"):
                st.session_state[f"export_{job_id}"] = (
                    fmt, len(reviews), reviews_to_bytes(reviews, fmt),
                )
        export = st.session_state.get(f"export_{job_id}")
        if export and export[0] == fmt:
            mimetypes = {"csv": "text/csv", "json": "application/json",
                         "xlsx": "application/vnd.openxmlformats-officedocument.spreadsheetml.sheet"}
            st.download_button(f"Download {export[1]} saved reviews", data=export[2],
                               file_name=f"{job['config']['app_id']}_reviews.{fmt}",
                               mime=mimetypes[fmt], key=f"download_{job_id}")
    st.download_button("Download coverage report", data=json.dumps(job, ensure_ascii=False, indent=2),
                       file_name=f"{job_id}_coverage.json", mime="application/json",
                       key=f"coverage_{job_id}")


if st.session_state.job_ids:
    selected = st.selectbox("Saved runs in this session", st.session_state.job_ids,
                            key="selected_job")
    show_job(selected)

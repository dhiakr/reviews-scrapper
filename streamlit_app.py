"""Streamlit frontend for the review scraper.

Paste a Google Play or Apple App Store URL; the app auto-detects the platform,
app id, and (for Apple) the storefront country, lets you pick countries /
languages, scrapes the retrievable public reviews, and offers a JSON or Excel
download.

Run locally:   python -m streamlit run streamlit_app.py
Deploy:        point Streamlit Community Cloud at this file.
"""

from __future__ import annotations

import os
import sys

import streamlit as st

# The backend modules use flat imports (``from normalizer import ...``), so the
# package directory must be on sys.path.
PACKAGE_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "review_scraper")
if PACKAGE_DIR not in sys.path:
    sys.path.insert(0, PACKAGE_DIR)

from exporters import reviews_to_bytes  # noqa: E402
from normalizer import deduplicate  # noqa: E402
from stores import app_store, google_play  # noqa: E402
from stores.app_store import ALL_COUNTRIES  # noqa: E402
from url_parser import (  # noqa: E402
    PLATFORM_APP_STORE,
    PLATFORM_GOOGLE_PLAY,
    UrlParseError,
    parse_url,
)

PLATFORM_LABELS = {
    PLATFORM_GOOGLE_PLAY: "Google Play",
    PLATFORM_APP_STORE: "Apple App Store",
}


# --------------------------------------------------------------------------- helpers

def parse_codes(raw: str) -> list[str]:
    """Split a comma/space separated list of country/language codes."""
    parts = raw.replace(",", " ").split()
    seen = []
    for code in parts:
        code = code.strip().lower()
        if code and code not in seen:
            seen.append(code)
    return seen


def run_scrape(parsed, countries, languages, max_reviews, newest_first, progress=None):
    """Scrape each country (and language for Play), isolating failures.

    ``progress`` is an optional callback ``(fraction, label)`` for a progress bar.
    Returns ``(reviews, failures)`` where failures is a list of (label, error).
    """
    collected = []
    failures = []

    # Build the full list of work items first so we can report progress.
    if parsed.platform == PLATFORM_GOOGLE_PLAY:
        languages = languages or ["en"]
        targets = [(c, lng) for c in countries for lng in languages]
    else:
        targets = [(c, None) for c in countries]

    total = max(len(targets), 1)
    for i, (country, language) in enumerate(targets):
        label = f"{country}/{language}" if language else country
        if progress:
            progress(i / total, label)
        try:
            if parsed.platform == PLATFORM_GOOGLE_PLAY:
                collected.extend(
                    google_play.scrape(
                        app_id=parsed.app_id,
                        source_url=parsed.source_url,
                        country=country,
                        language=language,
                        max_reviews=max_reviews,
                        newest_first=newest_first,
                    )
                )
            else:
                collected.extend(
                    app_store.scrape(
                        app_id=parsed.app_id,
                        source_url=parsed.source_url,
                        country=country,
                        max_reviews=max_reviews,
                    )
                )
        except Exception as exc:  # noqa: BLE001 - isolate per target
            failures.append((label, str(exc)))

    if progress:
        progress(1.0, "done")
    return deduplicate(collected), failures


# --------------------------------------------------------------------------- styling

st.set_page_config(page_title="App Review Scraper", page_icon="📲", layout="centered")

st.markdown(
    """
    <style>
      /* Hide default chrome (menu, footer, toolbar + Deploy button). */
      #MainMenu, footer {visibility: hidden;}
      header[data-testid="stHeader"] {display: none;}
      [data-testid="stToolbar"], [data-testid="stAppDeployButton"] {display: none;}

      .block-container {max-width: 760px; padding-top: 4.5rem;}

      .hero-title {text-align: center; font-weight: 800; font-size: 3rem;
                   letter-spacing: -0.02em; margin: 0 0 0.4rem 0;}
      .hero-sub {text-align: center; color: #6b7280; font-size: 1.02rem;
                 max-width: 560px; margin: 0 auto 1.6rem auto; line-height: 1.5;}

      /* Pill-shaped URL input, like the mockup search box. */
      div[data-testid="stTextInput"] input {
          border-radius: 999px; padding: 0.85rem 1.3rem; font-size: 1rem;
          border: 1px solid #e5e7eb;
          box-shadow: 0 1px 3px rgba(0,0,0,0.05);
      }
      div[data-testid="stTextInput"] input:focus {
          border-color: #6366f1; box-shadow: 0 0 0 3px rgba(99,102,241,0.15);
      }

      /* Example chips row. */
      div[data-testid="stButton"] > button {
          border-radius: 999px; border: 1px solid #e5e7eb;
          padding: 0.35rem 0.95rem; font-weight: 500;
      }
      div[data-testid="stButton"] > button[kind="primary"] {
          border: none; padding: 0.6rem 1.4rem;
      }
    </style>
    """,
    unsafe_allow_html=True,
)


# --------------------------------------------------------------------------- hero

st.markdown('<h1 class="hero-title">App Review Scraper</h1>', unsafe_allow_html=True)
st.markdown(
    '<p class="hero-sub">Collect retrievable public reviews from Google Play or '
    "the Apple App Store. Paste a store URL and export to Excel or JSON.</p>",
    unsafe_allow_html=True,
)

url = st.text_input(
    "Store URL",
    key="url_input",
    placeholder="https://play.google.com/store/apps/details?id=com.example.app",
    label_visibility="collapsed",
)


# --------------------------------------------------------------------------- detect

parsed = None
if url and url.strip():
    try:
        parsed = parse_url(url)
    except UrlParseError as exc:
        st.error(f"Could not parse URL: {exc}")

if parsed:
    st.divider()
    cols = st.columns(3)
    cols[0].metric("Platform", PLATFORM_LABELS[parsed.platform])
    cols[1].metric("App ID", parsed.app_id)
    cols[2].metric("Country in URL", (parsed.country or "—").upper())

    is_google = parsed.platform == PLATFORM_GOOGLE_PLAY

    all_label = (
        f"Sweep ALL {len(ALL_COUNTRIES)} storefronts to maximize total reviews "
        "(slower)"
    )
    if not is_google:
        all_label = (
            f"Sweep ALL {len(ALL_COUNTRIES)} storefronts — recommended for the App "
            "Store, which caps at ~500 reviews per country (slower)"
        )
    sweep_all = st.checkbox(all_label, value=not is_google)

    default_countries = parsed.country or "us"
    countries_raw = st.text_input(
        "Countries (comma or space separated)",
        value=default_countries,
        disabled=sweep_all,
        help="e.g. us gb pt — each is scraped separately and merged. Ignored when "
        "'Sweep ALL storefronts' is on.",
    )

    languages_raw = ""
    if is_google:
        languages_raw = st.text_input(
            "Languages (Google Play only)",
            value="en",
            help="e.g. en pt — the App Store is storefront/country based.",
        )

    col_a, col_b = st.columns(2)
    fetch_all = col_a.checkbox("Fetch all retrievable reviews", value=True)
    max_reviews = None
    if not fetch_all:
        max_reviews = col_b.number_input(
            "Max per country/language", min_value=1, value=200, step=50
        )

    newest_first = True
    if is_google:
        newest_first = st.checkbox("Newest reviews first", value=True)

    out_format = st.radio(
        "Download format", ["Excel (.xlsx)", "JSON (.json)"], horizontal=True
    )

    if st.button("Scrape reviews", type="primary", use_container_width=True):
        if sweep_all:
            countries = list(ALL_COUNTRIES)
        else:
            countries = parse_codes(countries_raw)
        languages = parse_codes(languages_raw)
        if not countries:
            st.error("Please enter at least one country code.")
            st.stop()

        bar = st.progress(0.0, text="Starting…")

        def _progress(frac, label):
            bar.progress(
                min(frac, 1.0), text=f"Scraping {label}…  ({int(frac * 100)}%)"
            )

        with st.spinner("Scraping reviews… this can take a while for large apps."):
            reviews, failures = run_scrape(
                parsed, countries, languages, max_reviews, newest_first,
                progress=_progress,
            )
        bar.empty()

        # Keep results in session so the download button doesn't re-scrape.
        st.session_state["reviews"] = reviews
        st.session_state["failures"] = failures
        st.session_state["app_id"] = parsed.app_id
        st.session_state["format"] = out_format


# --------------------------------------------------------------------------- results

if "reviews" in st.session_state:
    reviews = st.session_state["reviews"]
    failures = st.session_state.get("failures", [])
    app_id = st.session_state.get("app_id", "reviews")
    out_format = st.session_state.get("format", "Excel (.xlsx)")

    st.divider()
    st.success(f"Collected {len(reviews)} unique reviews.")

    for label, err in failures:
        st.warning(f"Failed for {label}: {err}")

    if reviews:
        st.dataframe(reviews[:200], use_container_width=True)
        if len(reviews) > 200:
            st.caption(f"Showing first 200 of {len(reviews)} rows. Download for all.")

        safe_id = app_id.replace(".", "_")
        if out_format.startswith("Excel"):
            st.download_button(
                "⬇️  Download Excel",
                data=reviews_to_bytes(reviews, "xlsx"),
                file_name=f"{safe_id}_reviews.xlsx",
                mime="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet",
                use_container_width=True,
            )
        else:
            st.download_button(
                "⬇️  Download JSON",
                data=reviews_to_bytes(reviews, "json"),
                file_name=f"{safe_id}_reviews.json",
                mime="application/json",
                use_container_width=True,
            )
    else:
        st.info(
            "No reviews were returned. For the App Store this is common — Apple's "
            "public RSS feed often returns an empty result. Google Play is usually "
            "more reliable."
        )

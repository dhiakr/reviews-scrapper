"""CSV, JSON and Excel exporters for normalized reviews.

Every exporter deduplicates before writing so exported files never contain
duplicate rows, regardless of how the reviews were collected.
"""

from __future__ import annotations

import csv
import io
import json
from typing import Any, Dict, List

from normalizer import REVIEW_FIELDS, deduplicate


def _infer_format(path: str) -> str:
    lower = path.lower()
    if lower.endswith(".json"):
        return "json"
    if lower.endswith(".csv"):
        return "csv"
    if lower.endswith(".xlsx"):
        return "xlsx"
    raise ValueError(
        f"Cannot infer export format from {path!r}; use a .csv, .json or .xlsx "
        f"extension."
    )


def _ordered_fieldnames(rows: List[Dict[str, Any]]) -> List[str]:
    """Canonical schema fields first, then any extra columns seen on rows."""
    extra: List[str] = []
    for row in rows:
        for key in row:
            if key not in REVIEW_FIELDS and key not in extra:
                extra.append(key)
    return [*REVIEW_FIELDS, *extra]


def export_reviews(reviews: List[Dict[str, Any]], path: str, fmt: str | None = None) -> int:
    """Export reviews to ``path``. Format inferred from extension unless given.

    Returns the number of rows written (after deduplication).
    """
    fmt = (fmt or _infer_format(path)).lower()
    rows = deduplicate(reviews)
    data = reviews_to_bytes(rows, fmt, _already_deduped=True)
    mode = "w" if fmt == "json" else "wb"
    if fmt == "json":
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(data.decode("utf-8"))
    else:
        with open(path, "wb") as fh:
            fh.write(data)
    return len(rows)


def reviews_to_bytes(
    reviews: List[Dict[str, Any]],
    fmt: str,
    *,
    _already_deduped: bool = False,
) -> bytes:
    """Serialize reviews to ``bytes`` in the given format (csv/json/xlsx).

    Handy for the Streamlit download buttons, which need bytes in memory rather
    than a file on disk. Always deduplicates unless told the input already is.
    """
    fmt = fmt.lower()
    if fmt == "excel":
        fmt = "xlsx"
    rows = reviews if _already_deduped else deduplicate(reviews)
    if fmt == "json":
        return json.dumps(rows, ensure_ascii=False, indent=2).encode("utf-8")
    if fmt == "csv":
        return _rows_to_csv_bytes(rows)
    if fmt == "xlsx":
        return _rows_to_xlsx_bytes(rows)
    raise ValueError(f"Unsupported export format: {fmt!r}")


def _rows_to_csv_bytes(rows: List[Dict[str, Any]]) -> bytes:
    fieldnames = _ordered_fieldnames(rows)
    buf = io.StringIO()
    writer = csv.DictWriter(buf, fieldnames=fieldnames, extrasaction="ignore")
    writer.writeheader()
    for row in rows:
        writer.writerow({key: row.get(key, "") for key in fieldnames})
    # utf-8-sig so Excel opens accented characters (e.g. Portuguese) correctly.
    return buf.getvalue().encode("utf-8-sig")


def _rows_to_xlsx_bytes(rows: List[Dict[str, Any]]) -> bytes:
    # openpyxl is imported lazily so CSV/JSON-only usage doesn't require it.
    from openpyxl import Workbook

    fieldnames = _ordered_fieldnames(rows)
    wb = Workbook()
    ws = wb.active
    ws.title = "reviews"
    ws.append(fieldnames)
    for row in rows:
        ws.append([_xlsx_cell(row.get(key)) for key in fieldnames])
    buf = io.BytesIO()
    wb.save(buf)
    return buf.getvalue()


def _xlsx_cell(value: Any) -> Any:
    """Coerce a value into something openpyxl can write to a cell."""
    if value is None:
        return ""
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def load_reviews(path: str, fmt: str | None = None) -> List[Dict[str, Any]]:
    """Load reviews from a CSV or JSON file produced by this tool.

    Used by the AI grouping step so it can post-process collected reviews
    without re-scraping anything.
    """
    fmt = (fmt or _infer_format(path)).lower()
    if fmt == "json":
        with open(path, "r", encoding="utf-8") as fh:
            data = json.load(fh)
        if not isinstance(data, list):
            raise ValueError(f"Expected a JSON array of reviews in {path!r}.")
        return data
    if fmt == "csv":
        with open(path, "r", newline="", encoding="utf-8") as fh:
            return list(csv.DictReader(fh))
    raise ValueError(f"Unsupported input format: {fmt!r}")

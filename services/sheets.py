"""Every Google Sheet read and write.

The Sheet is Storyline's database (Render's free filesystem is wiped on every
restart), and each write is also the "Actions" superpower.
"""

import json
import logging
import threading
import time
from datetime import datetime, timezone

import gspread

from services import config

log = logging.getLogger(__name__)

# Tab name -> column headers, in order. Row 1 of each tab holds these.
TABS = {
    "stories": ["timestamp", "title", "summary", "people", "places", "concern", "source", "call_id"],
    "photos": ["timestamp", "source", "description", "question", "asked"],
    "calls": ["timestamp", "phone_last4", "status", "duration_seconds", "ended_reason"],
}

TIMEOUT_SECONDS = 20
CACHE_SECONDS = 60

_lock = threading.Lock()
_spreadsheet = None
_cache: dict[str, tuple[float, list[dict]]] = {}


def now_iso() -> str:
    """UTC timestamp used in every row, e.g. 2026-11-09T02:15:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _open():
    global _spreadsheet
    with _lock:
        if _spreadsheet is None:
            info = json.loads(config.require("GOOGLE_SERVICE_ACCOUNT_JSON"))
            client = gspread.service_account_from_dict(info)
            client.set_timeout(TIMEOUT_SECONDS)
            _spreadsheet = client.open_by_key(config.require("GOOGLE_SHEET_ID"))
        return _spreadsheet


def _worksheet(tab: str):
    return _open().worksheet(tab)


def ensure_tabs() -> None:
    """Create any missing tabs and make sure each one has its header row."""
    spreadsheet = _open()
    existing = {ws.title: ws for ws in spreadsheet.worksheets()}
    for tab, headers in TABS.items():
        ws = existing.get(tab)
        if ws is None:
            ws = spreadsheet.add_worksheet(title=tab, rows=1000, cols=len(headers))
            log.info("Created tab %s", tab)
        if ws.row_values(1) != headers:
            ws.update([headers], "A1")
            ws.freeze(rows=1)
            log.info("Wrote headers on tab %s", tab)


def append(tab: str, record: dict) -> None:
    """Append one row. Values are stored as plain text (RAW), so text that
    starts with '=' can never run as a formula."""
    headers = TABS[tab]
    row = [str(record.get(h, "")) for h in headers]
    _worksheet(tab).append_row(row, value_input_option="RAW")
    _cache.pop(tab, None)


def rows(tab: str, use_cache: bool = False) -> list[dict]:
    """All data rows of a tab as dicts keyed by header. With use_cache, the
    result may be up to CACHE_SECONDS old (for page loads)."""
    if use_cache:
        hit = _cache.get(tab)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    # numericise_ignore=["all"] keeps every value as text, so a phone_last4
    # like "0412" doesn't turn into the number 412.
    records = _worksheet(tab).get_all_records(
        expected_headers=TABS[tab], numericise_ignore=["all"]
    )
    _cache[tab] = (time.time(), records)
    return records


def update_cell(tab: str, row_number: int, column: str, value) -> None:
    """Set one cell. row_number is the Sheet row (data starts at row 2)."""
    col = TABS[tab].index(column) + 1
    _worksheet(tab).update_cell(row_number, col, str(value))
    _cache.pop(tab, None)


def sheet_url() -> str:
    sheet_id = config.get("GOOGLE_SHEET_ID")
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}" if sheet_id else ""

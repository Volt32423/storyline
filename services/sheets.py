"""Every Google Sheet read and write.

The Sheet is Storyline's database (Render's free filesystem is wiped on every
restart), and each write is also the "Actions" superpower.

The app reaches the Sheet through a small Apps Script web app that lives
inside the Sheet (apps_script/Code.gs). That means no Google Cloud project
and no service account key: just a URL and a shared secret.
"""

import logging
import time
from datetime import datetime, timezone

import requests

from services import config

log = logging.getLogger(__name__)

# Tab name -> column headers, in order. Row 1 of each tab holds these.
TABS = {
    "stories": ["timestamp", "title", "summary", "people", "places", "concern", "source", "call_id"],
    "photos": ["timestamp", "source", "description", "question", "asked"],
    "calls": ["timestamp", "phone_last4", "status", "duration_seconds", "ended_reason", "call_id"],
}

TIMEOUT_SECONDS = 30
CACHE_SECONDS = 60

_cache: dict[str, tuple[float, list[dict]]] = {}


def now_iso() -> str:
    """UTC timestamp used in every row, e.g. 2026-11-09T02:15:00Z."""
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def is_configured() -> bool:
    return bool(config.get("SHEETS_WEBAPP_URL") and config.get("SHEETS_WEBAPP_SECRET"))


def _call(action: str, **payload) -> dict:
    """POST one action to the Apps Script bridge and return its JSON reply.
    Apps Script answers a POST with a redirect, which requests follows."""
    body = {"secret": config.require("SHEETS_WEBAPP_SECRET"), "action": action, **payload}
    response = requests.post(config.require("SHEETS_WEBAPP_URL"), json=body, timeout=TIMEOUT_SECONDS)
    response.raise_for_status()
    try:
        data = response.json()
    except ValueError:
        snippet = response.text[:200].replace("\n", " ")
        raise RuntimeError(
            f"Sheet bridge didn't return JSON. Is SHEETS_WEBAPP_URL the /exec URL and "
            f"deployed with access 'Anyone'? Got: {snippet}"
        ) from None
    if not data.get("ok"):
        raise RuntimeError(f"Sheet bridge error on '{action}': {data.get('error')}")
    return data


def ping() -> None:
    _call("ping")


def ensure_tabs() -> None:
    """Create any missing tabs and make sure each one has its header row."""
    _call("ensure_tabs", tabs=TABS)


def append(tab: str, record: dict) -> None:
    """Append one row. Values are stored as plain text, never as formulas."""
    values = [str(record.get(h, "")) for h in TABS[tab]]
    _call("append", tab=tab, values=values)
    _cache.pop(tab, None)


def rows(tab: str, use_cache: bool = False) -> list[dict]:
    """All data rows of a tab as dicts keyed by header (every value is text),
    plus "_row": the row's number in the Sheet, for update_cell.
    With use_cache, the result may be up to CACHE_SECONDS old (for page loads)."""
    if use_cache:
        hit = _cache.get(tab)
        if hit and time.time() - hit[0] < CACHE_SECONDS:
            return hit[1]
    values = _call("rows", tab=tab)["values"]
    headers = TABS[tab]
    records = []
    for index, row in enumerate(values[1:], start=2):
        if not any(cell.strip() for cell in row):
            continue
        record = dict(zip(headers, row + [""] * (len(headers) - len(row))))
        record["_row"] = index
        records.append(record)
    _cache[tab] = (time.time(), records)
    return records


def update_cell(tab: str, row_number: int, column: str, value) -> None:
    """Set one cell. row_number is the Sheet row (data starts at row 2)."""
    col = TABS[tab].index(column) + 1
    _call("update_cell", tab=tab, row=row_number, col=col, value=str(value))
    _cache.pop(tab, None)


def sheet_url() -> str:
    sheet_id = config.get("GOOGLE_SHEET_ID")
    return f"https://docs.google.com/spreadsheets/d/{sheet_id}" if sheet_id else ""

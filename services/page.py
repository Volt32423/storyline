"""Everything the home page shows, gathered so that a slow or broken service
never slows the page down. Sheet reads are cached for 60 seconds.

The page never waits more than PAGE_WAIT_SECONDS for the Sheet or the
weather. Reads run in the background, one per source at a time. If one isn't
back in time, the page shows the last rows it read and the read finishes for
the next visitor. That matters beyond speed: Render's deploy checks load the
page, and a page that hangs on a slow Google Apps Script gets the app
restarted over and over (a 502 for visitors each time)."""

import logging
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime

from services import calls, config, sheets, weather

log = logging.getLogger(__name__)

GITHUB_URL = "https://github.com/Volt32423/storyline"
PHOTOS_SHOWN = 6
PAGE_WAIT_SECONDS = 3.0
SOURCES = ("stories", "photos", "calls")

# How a source came back for this page load.
OK = "ok"            # rows are here (fresh, or the last ones read)
LOADING = "loading"  # nothing read yet, and the read is still running
DOWN = "down"        # the Sheet isn't set up, or the read failed with nothing to fall back on

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="page-data")
_jobs: dict[str, Future] = {}
_jobs_lock = threading.Lock()


def _start(key: str, fn) -> Future:
    """Run fn in the background unless the same read is already running."""
    with _jobs_lock:
        job = _jobs.get(key)
        if job is None or job.done():
            job = _pool.submit(fn)
            _jobs[key] = job
        return job


def _read(tab: str) -> list[dict]:
    """Read a tab, logging how long a slow read took, or why it failed, so
    Render's logs show whether Apps Script is slow or not answering at all."""
    started = time.monotonic()
    try:
        found = sheets.rows(tab)
    except Exception as error:
        log.error("Sheet read for %s failed after %.1f s: %s", tab, time.monotonic() - started, error)
        raise
    took = time.monotonic() - started
    if took > PAGE_WAIT_SECONDS:
        log.info("Sheet read for %s took %.1f s (%d rows); the page has it now", tab, took, len(found))
    return found


def warm() -> None:
    """Start reading everything the page needs, without waiting. Runs when
    the app starts, so the first visitor after a deploy gets a full page."""
    if sheets.is_configured():
        for tab in SOURCES:
            if sheets.fresh(tab) is None:
                _start(tab, lambda tab=tab: _read(tab))
    _start("weather", weather.current)


def _rows(tab: str, deadline: float) -> tuple[list[dict], str]:
    """(rows, OK / LOADING / DOWN) for a tab, never waiting past the deadline.
    Fresh cached rows come back at once. Otherwise a background read starts
    (or is joined), and if it isn't back by the deadline, the last rows read
    are used instead."""
    if not sheets.is_configured():
        return [], DOWN
    hit = sheets.fresh(tab)
    if hit is not None:
        return hit, OK
    job = _start(tab, lambda: _read(tab))
    try:
        return job.result(timeout=max(0.0, deadline - time.monotonic())), OK
    except FutureTimeout:
        stale = sheets.cached(tab)
        if stale is not None:
            return stale, OK
        log.warning("Sheet slow to answer for %s; page shows it as loading", tab)
        return [], LOADING
    except Exception:
        stale = sheets.cached(tab)
        return (stale, OK) if stale is not None else ([], DOWN)


def _weather(deadline: float) -> dict | None:
    """Current weather, never waiting past the deadline (then the last reading,
    if there is one)."""
    job = _start("weather", weather.current)
    try:
        return job.result(timeout=max(0.0, deadline - time.monotonic()))
    except Exception:
        return weather.last()


def friendly_time(timestamp: str) -> str:
    """'2026-11-09T21:12:00Z' -> 'today at 4:12 PM', 'yesterday at ...' or
    'Nov 7 at ...', in Eastern time (Frank lives in Pittsburgh)."""
    try:
        moment = datetime.fromisoformat((timestamp or "").replace("Z", "+00:00"))
    except ValueError:
        return ""
    zone = calls._local_zone()
    local = moment.astimezone(zone) if zone else moment
    clock = f"{local.hour % 12 or 12}:{local.minute:02d} {'AM' if local.hour < 12 else 'PM'}"
    days_ago = (datetime.now(local.tzinfo).date() - local.date()).days
    if days_ago == 0:
        return f"today at {clock}"
    if days_ago == 1:
        return f"yesterday at {clock}"
    return f"{local.strftime('%b')} {local.day} at {clock}"


def _local(timestamp: str) -> datetime | None:
    try:
        moment = datetime.fromisoformat((timestamp or "").replace("Z", "+00:00"))
    except ValueError:
        return None
    zone = calls._local_zone()
    return moment.astimezone(zone) if zone else moment


def book_date(timestamp: str) -> str:
    """'2026-11-09T21:12:00Z' -> 'November 9, 2026' (Eastern). Empty if unknown."""
    local = _local(timestamp)
    return f"{local.strftime('%B')} {local.day}, {local.year}" if local else ""


def friendly_duration(seconds: str) -> str:
    """'130' -> '2 min 10 s'. Empty if unknown."""
    try:
        total = int(float(seconds))
    except (TypeError, ValueError):
        return ""
    minutes, secs = divmod(total, 60)
    return f"{minutes} min {secs} s" if minutes else f"{secs} s"


def pretty_phone(number: str) -> str:
    """'+14125550123' -> '(412) 555-0123'. Anything else comes back as is."""
    digits = "".join(ch for ch in number or "" if ch.isdigit())
    if len(digits) == 11 and digits.startswith("1"):
        return f"({digits[1:4]}) {digits[4:7]}-{digits[7:]}"
    return number or ""


def _count(n: int, one: str, many: str = "") -> str:
    """_count(1, 'photo') -> '1 photo'; _count(3, 'story', 'stories') -> '3 stories'."""
    return f"{n} {one if n == 1 else (many or one + 's')}"


def curly(text: str) -> str:
    """Typeset apostrophes for display: it's -> it’s. The call itself keeps
    plain text, since that's what the voice reads."""
    return re.sub(r"(\w)'(\w)", "\\1\u2019\\2", text or "")


def _shorten(text: str, limit: int) -> str:
    text = (text or "").strip()
    return text if len(text) <= limit else text[: limit - 3].rstrip() + "..."


def _plan_for_page(plan: dict) -> dict:
    """What the next call will bring up, in words a judge can play along with."""
    if plan["kind"] == "photo":
        return {"kind": "photo", "text": plan["photo"].get("question", "").strip()}
    if plan["kind"] == "story":
        story = plan["story"]
        return {"kind": "story", "text": story.get("title", "").strip(), "anchor": f"story-{story.get('_row', '')}"}
    return {"kind": "topic", "text": plan["topic"]}


def book_data() -> dict:
    """Everything templates/book.html needs: every story, oldest first, laid
    out like a keepsake. Concerns stay out; they're for the family to check,
    not for the book. Never raises."""
    rows, state = _rows("stories", time.monotonic() + PAGE_WAIT_SECONDS)
    stories = calls.memory_stories(rows)
    months = []
    for s in stories:
        local = _local(s.get("timestamp", ""))
        if local:
            months.append((local.year, local.month, f"{local.strftime('%B')} {local.year}"))
    months.sort()
    if not months:
        collected = ""
    elif months[0][2] == months[-1][2]:
        collected = months[0][2]
    else:
        collected = f"{months[0][2]} to {months[-1][2]}"
    entries = []
    for number, s in enumerate(stories, start=1):
        told_on = book_date(s.get("timestamp", ""))
        entries.append({
            "number": number,
            "title": s.get("title", "").strip() or "Untitled story",
            "summary": s.get("summary", "").strip(),
            "people": s.get("people", "").strip(),
            "places": s.get("places", "").strip(),
            "told": (f"Told on a call on {told_on}" if s.get("source") == "call" and told_on
                     else "One of the stories he told before Storyline"),
        })
    return {"name": config.GRANDPA_NAME, "stories": entries, "ok": state == OK, "loading": state == LOADING,
            "count": _count(len(entries), "story", "stories"), "collected": collected}


def page_data() -> dict:
    """Everything templates/index.html needs. Never raises, and never waits
    more than about PAGE_WAIT_SECONDS."""
    deadline = time.monotonic() + PAGE_WAIT_SECONDS
    warm()  # start every read at once, so they run side by side
    all_stories, stories_state = _rows("stories", deadline)
    photo_rows, photos_state = _rows("photos", deadline)
    call_rows, calls_state = _rows("calls", deadline)
    today = _weather(deadline)
    stories_ok, photos_ok, calls_ok = (state == OK for state in (stories_state, photos_state, calls_state))
    loading = LOADING in (stories_state, photos_state, calls_state)
    loading_line = "Loading from the family Sheet…"

    stories = calls.memory_stories(all_stories)
    plan = calls.choose_plan(stories, photo_rows, len(call_rows))
    last_call = call_rows[-1] if call_rows else None
    last_photo = photo_rows[-1] if photo_rows else None
    texted = [p for p in photo_rows if p.get("source") == "text"]
    linq_number = pretty_phone(config.get("LINQ_PHONE_NUMBER"))
    sheet_url = sheets.sheet_url()
    sheet_ok = stories_ok and photos_ok and calls_ok
    city = weather.city_name()
    name = config.GRANDPA_NAME

    if calls_state == LOADING:
        voice = loading_line
    elif last_call:
        voice = f"Last call {friendly_time(last_call.get('timestamp', ''))}"
        duration = friendly_duration(last_call.get("duration_seconds", ""))
        voice += f", {duration} long." if duration else "."
    else:
        voice = "No calls yet. Yours can be the first."

    if not linq_number:
        texting_line = "Texting isn't switched on yet. The upload form below works the same way."
    elif texted:
        texting_line = f"Last photo texted in {friendly_time(texted[-1].get('timestamp', ''))}."
    else:
        texting_line = f"Text a photo to {linq_number} and it shows up below."

    saved = _count(len(stories), "saved story", "saved stories")
    if stories_state == LOADING or photos_state == LOADING:
        knowledge_line = loading_line
    elif plan["kind"] == "photo":
        knowledge_line = f"Next call asks about the newest photo, with {saved} in mind."
    elif plan["kind"] == "story":
        knowledge_line = f"Next call retells “{plan['story'].get('title', '').strip()}” ({saved} in all)."
    else:
        knowledge_line = f"Next call starts a new story about {plan['topic']}."

    # The newest thing Claude did on its own: a story it saved with its
    # save_story tool, and whether it flagged a concern for the family.
    latest_saved = next((s for s in reversed(stories) if s.get("source") == "call"), None)
    counts = (f"{_count(len(stories), 'story', 'stories')}, {_count(len(photo_rows), 'photo')} "
              f"and {_count(len(call_rows), 'call')} saved so far.")
    if loading:
        actions_line = loading_line
    elif not sheet_ok:
        actions_line = "The Sheet isn't answering right now. Refresh in a moment."
    elif latest_saved:
        actions_line = (f"Latest: Claude saved “{latest_saved.get('title', '').strip()}” "
                        f"{friendly_time(latest_saved.get('timestamp', ''))}"
                        f"{' and flagged a concern for the family' if latest_saved.get('concern', '').strip() else ''}. "
                        f"{counts}")
    else:
        actions_line = counts

    powers = [
        {"name": "Voice", "ok": bool(last_call),
         "what": f"Storyline phones you and has a slow, patient conversation as if you were {name}.",
         "evidence": voice},
        {"name": "Texting", "ok": bool(linq_number),
         "what": "Family members text in old photos, and get each new story back by text.",
         "evidence": texting_line},
        {"name": "Vision", "ok": bool(last_photo),
         "what": "Claude looks at each photo and writes down what it sees, plus one gentle question.",
         "evidence": (loading_line if photos_state == LOADING
                      else f"Latest: “{_shorten(last_photo.get('description', ''), 120)}”"
                      if last_photo else "No photos yet. Text or upload one below.")},
        {"name": "Knowledge", "ok": stories_ok,
         "what": (f"Before every call, Storyline reads {name}'s life profile, a knowledge file, "
                  f"plus every story he has told, so it can retell them and fill in what he forgets."),
         "evidence": knowledge_line},
        {"name": "Real-world data", "ok": bool(today),
         "what": f"Every call opens with today's weather in {city}, from OpenWeather.",
         "evidence": (f"Right now in {city}: {today['temp']}°F and {today['description']}."
                      if today else "The weather is unavailable right now, so calls skip it.")},
        {"name": "Actions", "ok": sheet_ok,
         "what": ("After each call, Claude saves the story with a tool call and flags anything the "
                  "family should check on. Every story, photo and call goes into the family's Google Sheet."),
         "evidence": actions_line,
         "link": sheet_url},
    ]

    photos = [{
        "description": p.get("description", "").strip(),
        "question": p.get("question", "").strip(),
        "asked": p.get("asked", "").upper() == "TRUE",
        "how": "Texted in" if p.get("source") == "text" else "Uploaded",
        "when": friendly_time(p.get("timestamp", "")),
    } for p in reversed(photo_rows[-PHOTOS_SHOWN:])]

    story_book = [{
        "anchor": f"story-{s.get('_row', '')}",
        "title": s.get("title", "").strip() or "Untitled story",
        "summary": s.get("summary", "").strip(),
        "people": s.get("people", "").strip(),
        "places": s.get("places", "").strip(),
        "concern": s.get("concern", "").strip(),
        "told": (f"Told on a call {friendly_time(s.get('timestamp', ''))}"
                 if s.get("source") == "call" else "One of the stories he told before Storyline"),
    } for s in reversed(stories)]

    return {
        "name": name,
        "city": city,
        "opening_line": calls.first_message({"weather": today}),
        "next": ({"kind": "loading"} if LOADING in (stories_state, photos_state) else _plan_for_page(plan)),
        "loading": loading,
        "photos_loading": photos_state == LOADING,
        "stories_loading": stories_state == LOADING,
        "powers": powers,
        "photos": photos,
        "photo_count": len(photo_rows),
        "stories": story_book,
        "linq_number": linq_number,
        "linq_sms": "sms:" + config.get("LINQ_PHONE_NUMBER") if linq_number else "",
        "sheet_url": sheet_url,
        "github_url": config.get("GITHUB_URL", GITHUB_URL),
    }

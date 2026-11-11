"""Everything the home page shows, gathered so that a slow or broken service
never slows the page down. Sheet reads are cached for 60 seconds.

The page never waits more than PAGE_WAIT_SECONDS for the Sheet or the
weather. Reads run in the background, one per source at a time. If one isn't
back in time, the page shows the last rows it read and the read finishes for
the next visitor. That matters beyond speed: Render's deploy checks load the
page, and a page that hangs on a slow Google Apps Script gets the app
restarted over and over (a 502 for visitors each time)."""

import hashlib
import logging
import os
import re
import threading
import time
from concurrent.futures import Future, ThreadPoolExecutor
from concurrent.futures import TimeoutError as FutureTimeout
from datetime import datetime

from services import calls, config, sheets, texting, weather

log = logging.getLogger(__name__)

GITHUB_URL = "https://github.com/Volt32423/storyline"
PHOTOS_SHOWN = 6
PAGE_WAIT_SECONDS = 4.0  # only ever waited on a cold start; a normal Apps Script read takes 3-4 s
SOURCES = ("stories", "photos", "calls")

# How a source came back for this page load.
OK = "ok"            # rows are here (fresh, or the last ones read)
LOADING = "loading"  # nothing read yet, and the read is still running
DOWN = "down"        # the Sheet isn't set up, or the read failed with nothing to fall back on

_pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="page-data")
_jobs: dict[str, Future] = {}
_jobs_lock = threading.Lock()


def _fresh_after_fork() -> None:
    """Render imports the app once in gunicorn's main process, then forks the
    worker that serves pages. Threads don't survive a fork, so the worker's
    copy of the pool, its jobs and its lock would never finish anything (the
    page said "Loading" forever). Give the worker its own."""
    global _pool, _jobs, _jobs_lock
    _pool = ThreadPoolExecutor(max_workers=4, thread_name_prefix="page-data")
    _jobs = {}
    _jobs_lock = threading.Lock()


os.register_at_fork(after_in_child=_fresh_after_fork)


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

    Fresh cached rows come back at once. Older cached rows also come back at
    once, while a background read refreshes them for the next visitor (Apps
    Script often takes 3-4 s, and nobody should wait for that). Only when
    nothing has been read yet, just after a restart, does the page wait for the
    read, up to the deadline."""
    if not sheets.is_configured():
        return [], DOWN
    hit = sheets.fresh(tab)
    if hit is not None:
        return hit, OK
    job = _start(tab, lambda: _read(tab))
    stale = sheets.cached(tab)
    if stale is not None:
        return stale, OK
    try:
        return job.result(timeout=max(0.0, deadline - time.monotonic())), OK
    except FutureTimeout:
        log.warning("Sheet slow to answer for %s; page shows it as loading", tab)
        return [], LOADING
    except Exception:
        return [], DOWN


def _weather(deadline: float) -> dict | None:
    """Current weather, never waiting past the deadline (then the last reading,
    if there is one)."""
    job = _start("weather", weather.current)
    last = weather.last()
    if last is not None and not job.done():
        return last  # a refresh is running; use the last reading rather than wait
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


def photo_id(photo: dict) -> str:
    """A photo's id on the page and in the book: its row in the Sheet."""
    return f"photo-{photo.get('_row') or photo.get('timestamp', '')}"


def photo_links(photo_rows: list, stories: list, call_rows: list) -> tuple[dict, dict]:
    """Which story each photo led to. A call that asked about a photo has a
    topic like "Asked about a photo: <start of Claude's description>", and the
    story saved after that call carries the call's id. Returns
    (story by photo_id, photo by call id). Never raises."""
    story_by_call = {str(s.get("call_id", "")): s for s in stories
                     if s.get("source") == "call" and s.get("call_id")}
    story_by_photo, photo_by_call = {}, {}
    for row in call_rows:
        topic = str(row.get("topic", ""))
        call_id = str(row.get("call_id", ""))
        if not topic.startswith(calls.PHOTO_TOPIC) or not call_id:
            continue
        start = topic[len(calls.PHOTO_TOPIC):].removesuffix("...").strip()
        called_at = str(row.get("timestamp", ""))
        # The photo it asked about was sent before the call and is marked
        # asked; if the same picture was sent twice, take the newest such one.
        matches = [p for p in photo_rows
                   if start and str(p.get("description", "")).strip().startswith(start)
                   and (not called_at or str(p.get("timestamp", "")) <= called_at)]
        asked = [p for p in matches if str(p.get("asked", "")).upper() == "TRUE"]
        photo = (asked or matches or [None])[-1]
        if not photo:
            continue
        photo_by_call[call_id] = photo
        if call_id in story_by_call:
            story_by_photo[photo_id(photo)] = story_by_call[call_id]
    return story_by_photo, photo_by_call


def live_version() -> str:
    """A short fingerprint of the Sheet rows the page is built from, taken from
    the rows already in memory: it never waits and never reads the Sheet.
    Every save here updates those rows at once, so it changes as soon as a
    photo, story or call is saved, or a photo is asked about."""
    seen = "|".join(repr(sheets.cached(tab)) for tab in SOURCES)
    return hashlib.sha1(seen.encode()).hexdigest()[:12]


def _plan_for_page(plan: dict) -> dict:
    """What the next call will bring up, in words a judge can play along with."""
    if plan["kind"] == "photo":
        return {"kind": "photo", "text": plan["photo"].get("question", "").strip()}
    if plan["kind"] == "story":
        story = plan["story"]
        return {"kind": "story", "text": story.get("title", "").strip(), "anchor": f"story-{story.get('_row', '')}"}
    return {"kind": "topic", "text": plan["topic"]}


def book_data() -> dict:
    """Everything templates/book.html needs, laid out like a keepsake: every
    story, oldest first, then every photo the family sent with what Claude saw
    in it. A story told about a photo says so, and the photo names its story.
    Concerns stay out; they're for the family to check, not for the book.
    Never raises, and never waits more than about PAGE_WAIT_SECONDS."""
    deadline = time.monotonic() + PAGE_WAIT_SECONDS
    warm()  # start every read at once, so they run side by side
    rows, state = _rows("stories", deadline)
    photo_rows, photos_state = _rows("photos", deadline)
    call_rows, _ = _rows("calls", deadline)
    stories = calls.memory_stories(rows)
    story_by_photo, photo_by_call = photo_links(photo_rows, stories, call_rows)

    months = []
    for row in stories + photo_rows:
        local = _local(row.get("timestamp", ""))
        if local:
            months.append((local.year, local.month, f"{local.strftime('%B')} {local.year}"))
    months.sort()
    if not months:
        collected = ""
    elif months[0][2] == months[-1][2]:
        collected = months[0][2]
    else:
        collected = f"{months[0][2]} to {months[-1][2]}"

    entries, number_of = [], {}
    for number, s in enumerate(stories, start=1):
        number_of[id(s)] = number
        told_on = book_date(s.get("timestamp", ""))
        photo = photo_by_call.get(str(s.get("call_id", "")))
        entries.append({
            "number": number,
            "title": s.get("title", "").strip() or "Untitled story",
            "summary": s.get("summary", "").strip(),
            "people": s.get("people", "").strip(),
            "places": s.get("places", "").strip(),
            "told": (f"Told on a call on {told_on}" if s.get("source") == "call" and told_on
                     else "One of the stories he told before Storyline"),
            "photo": photo.get("description", "").strip() if photo else "",
        })

    pictures = []
    for number, p in enumerate(photo_rows, start=1):
        story = story_by_photo.get(photo_id(p))
        sent_on = book_date(p.get("timestamp", ""))
        how = "Texted in" if p.get("source") == "text" else "Uploaded"
        pictures.append({
            "number": number,
            "description": p.get("description", "").strip(),
            "question": p.get("question", "").strip(),
            "sent": f"{how} on {sent_on}" if sent_on else how,
            "story": ({"title": story.get("title", "").strip() or "Untitled story",
                       "number": number_of.get(id(story))} if story and id(story) in number_of else None),
            "asked": p.get("asked", "").upper() == "TRUE",
        })

    count = _count(len(entries), "story", "stories")
    if pictures:
        count += f" and {_count(len(pictures), 'photo')}"
    return {"name": config.GRANDPA_NAME, "stories": entries, "photos": pictures,
            "ok": state == OK, "loading": state == LOADING, "photos_loading": photos_state == LOADING,
            "count": count, "collected": collected}


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
    story_by_photo, photo_by_call = photo_links(photo_rows, stories, call_rows)
    text_number = pretty_phone(texting.phone_number())
    # Sendblue's free plan only passes on texts from the family's verified
    # phones, so judges are pointed at the upload form instead of the number.
    text_open = bool(text_number) and texting.open_to_everyone()
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

    if not text_number:
        texting_line = "Texting isn't switched on yet. The upload form below works the same way."
    elif texted:
        texting_line = f"Last photo texted in {friendly_time(texted[-1].get('timestamp', ''))}."
    elif text_open:
        texting_line = f"Text a photo to {text_number} and it shows up below."
    else:
        texting_line = "Ready for the family's phones. A photo they text in shows up below."

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
        {"name": "Texting", "ok": bool(text_number),
         "what": "Family members text in old photos, and get each new story back by text.",
         "evidence": texting_line},
        {"name": "Vision", "ok": bool(last_photo),
         "what": "Claude looks at each photo and writes down what it sees, plus one gentle question.",
         "evidence": (loading_line if photos_state == LOADING
                      else f"Latest: “{_shorten(last_photo.get('description', ''), 120)}”"
                      if last_photo else f"No photos yet. {'Text or upload' if text_open else 'Upload'} one below.")},
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

    def anchor(story: dict) -> str:
        return f"story-{story.get('_row', '')}"

    photos = []
    for p in reversed(photo_rows[-PHOTOS_SHOWN:]):
        story = story_by_photo.get(photo_id(p))
        photos.append({
            "key": photo_id(p),
            "description": p.get("description", "").strip(),
            "question": p.get("question", "").strip(),
            "asked": p.get("asked", "").upper() == "TRUE",
            "how": "Texted in" if p.get("source") == "text" else "Uploaded",
            "when": friendly_time(p.get("timestamp", "")),
            "story": ({"title": story.get("title", "").strip() or "Untitled story", "anchor": anchor(story)}
                      if story else None),
        })

    story_book = []
    for s in reversed(stories):
        told = (f"Told on a call {friendly_time(s.get('timestamp', ''))}"
                if s.get("source") == "call" else "One of the stories he told before Storyline")
        if str(s.get("call_id", "")) in photo_by_call:
            told += ", about a photo the family sent"
        story_book.append({
            "anchor": anchor(s),
            "title": s.get("title", "").strip() or "Untitled story",
            "summary": s.get("summary", "").strip(),
            "people": s.get("people", "").strip(),
            "places": s.get("places", "").strip(),
            "concern": s.get("concern", "").strip(),
            "told": told,
        })

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
        "texting_on": bool(text_number),
        # Shown to everyone when anyone can text it (Linq); otherwise only to a
        # visitor who has typed the demo code (templates/index.html checks).
        "text_number": text_number if text_open else "",
        "text_sms": "sms:" + texting.phone_number() if text_open else "",
        "family_number": text_number,
        "family_sms": "sms:" + texting.phone_number() if text_number else "",
        "live_version": live_version(),
        "sheet_url": sheet_url,
        "github_url": config.get("GITHUB_URL", GITHUB_URL),
    }

"""Everything the home page shows, gathered so that a slow or broken service
never stops the page from loading. Sheet reads are cached for 60 seconds."""

import logging
import re
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime

from services import calls, config, sheets, weather

log = logging.getLogger(__name__)

GITHUB_URL = "https://github.com/Volt32423/storyline"
PHOTOS_SHOWN = 6


def _rows(tab: str) -> tuple[list[dict], bool]:
    """(rows, ok). If the Sheet can't be reached, fall back to the last rows
    read, so a hiccup at Google doesn't empty the page."""
    if not sheets.is_configured():
        return [], False
    try:
        return sheets.rows(tab, use_cache=True), True
    except Exception as error:
        stale = sheets.cached(tab)
        log.error("Page couldn't read %s from the Sheet: %s%s", tab, error,
                  " (showing older rows)" if stale is not None else "")
        return stale or [], stale is not None


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
    rows, ok = _rows("stories")
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
    return {"name": config.GRANDPA_NAME, "stories": entries, "ok": ok,
            "count": _count(len(entries), "story", "stories"), "collected": collected}


def page_data() -> dict:
    """Everything templates/index.html needs. Never raises."""
    with ThreadPoolExecutor(max_workers=4) as pool:
        stories_job = pool.submit(_rows, "stories")
        photos_job = pool.submit(_rows, "photos")
        calls_job = pool.submit(_rows, "calls")
        weather_job = pool.submit(weather.current)
        all_stories, stories_ok = stories_job.result()
        photo_rows, photos_ok = photos_job.result()
        call_rows, calls_ok = calls_job.result()
        today = weather_job.result()

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

    if last_call:
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
    if plan["kind"] == "photo":
        knowledge_line = f"Next call asks about the newest photo, with {saved} in mind."
    elif plan["kind"] == "story":
        knowledge_line = f"Next call retells “{plan['story'].get('title', '').strip()}” ({saved} in all)."
    else:
        knowledge_line = f"Next call starts a new story about {plan['topic']}."

    powers = [
        {"name": "Voice", "ok": bool(last_call),
         "what": f"Storyline phones you and has a slow, patient conversation as if you were {name}.",
         "evidence": voice},
        {"name": "Texting", "ok": bool(linq_number),
         "what": "Family members text in old photos, and get each new story back by text.",
         "evidence": texting_line},
        {"name": "Vision", "ok": bool(last_photo),
         "what": "Claude looks at each photo and writes down what it sees, plus one gentle question.",
         "evidence": (f"Latest: “{_shorten(last_photo.get('description', ''), 120)}”"
                      if last_photo else "No photos yet. Text or upload one below.")},
        {"name": "Knowledge", "ok": stories_ok,
         "what": f"Every call knows {name}'s life story and each story he has told before.",
         "evidence": knowledge_line},
        {"name": "Real-world data", "ok": bool(today),
         "what": f"Every call opens with today's weather in {city}, from OpenWeather.",
         "evidence": (f"Right now in {city}: {today['temp']}°F and {today['description']}."
                      if today else "The weather is unavailable right now, so calls skip it.")},
        {"name": "Actions", "ok": sheet_ok,
         "what": "Every story, photo and call is saved to the family's Google Sheet.",
         "evidence": (f"{_count(len(stories), 'story', 'stories')}, {_count(len(photo_rows), 'photo')} "
                      f"and {_count(len(call_rows), 'call')} saved so far."
                      if sheet_ok else "The Sheet can't be reached right now."),
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
        "next": _plan_for_page(plan),
        "powers": powers,
        "photos": photos,
        "photo_count": len(photo_rows),
        "stories": story_book,
        "linq_number": linq_number,
        "linq_sms": "sms:" + config.get("LINQ_PHONE_NUMBER") if linq_number else "",
        "sheet_url": sheet_url,
        "github_url": config.get("GITHUB_URL", GITHUB_URL),
    }

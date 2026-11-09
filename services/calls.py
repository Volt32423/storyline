"""Build the call prompt and start the Vapi outbound call (the Voice superpower).

Field names checked against docs.vapi.ai (Nov 2026):
- POST https://api.vapi.ai/call with phoneNumberId, customer.number and an
  inline (transient) `assistant`
- assistant: name, firstMessage, model {provider, model, messages, tools},
  voice {provider, voiceId}, maxDurationSeconds, server {url}, serverMessages
- hang-up tool: model.tools [{type: "endCall", messages: [{type: "request-start", content}]}]
- provider "anthropic" offers "claude-haiku-5-5"; provider "vapi" offers voice "Leah"
"""

import hashlib
import hmac
import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from services import claude_ai, config, knowledge, sheets, texting, weather

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20
MAX_CALL_SECONDS = 180
CALLS_PER_NUMBER_PER_DAY = 3
MIN_STORY_SECONDS = 20
WEBHOOK_HEADER = "X-Storyline-Secret"


def webhook_secret() -> str:
    """The value Vapi sends back in WEBHOOK_HEADER, so only Vapi can post
    call reports. Derived from SHEETS_WEBAPP_SECRET, so there's nothing extra
    to set up; VAPI_WEBHOOK_SECRET overrides it."""
    explicit = config.get("VAPI_WEBHOOK_SECRET")
    if explicit:
        return explicit
    base = config.get("SHEETS_WEBAPP_SECRET")
    if not base:
        return ""
    return hmac.new(base.encode(), b"storyline-vapi-webhook", hashlib.sha256).hexdigest()[:40]


def webhook_authorized(headers) -> bool:
    expected = webhook_secret()
    if not expected:
        log.warning("No webhook secret configured, so the Vapi webhook isn't checked")
        return True
    return hmac.compare_digest(headers.get(WEBHOOK_HEADER, ""), expected)


class CallError(Exception):
    """A problem the caller should see, with a short code for the page."""

    def __init__(self, code: str, detail: str = ""):
        super().__init__(detail or code)
        self.code = code


# ---------- phone numbers and limits ----------

def normalize_us_phone(raw: str) -> str | None:
    """'(412) 555-0123' or '+1 412 555 0123' -> '+14125550123'. US only."""
    digits = re.sub(r"\D", "", raw or "")
    if len(digits) == 11 and digits.startswith("1"):
        digits = digits[1:]
    if len(digits) != 10 or digits[0] in "01" or digits[3] in "01":
        return None
    return "+1" + digits


def _local_zone():
    try:
        return ZoneInfo("America/New_York")
    except Exception:
        return None


def _is_today(timestamp: str) -> bool:
    """Is a stored UTC timestamp (2026-11-09T02:15:00Z) from today, Eastern time?"""
    try:
        moment = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
    except ValueError:
        return False
    zone = _local_zone()
    now = datetime.now(zone) if zone else datetime.now(moment.tzinfo)
    return moment.astimezone(now.tzinfo).date() == now.date()


def check_limits(phone_e164: str) -> None:
    """Raise CallError if today's caps are reached."""
    today = [row for row in sheets.rows("calls") if _is_today(row.get("timestamp", ""))]
    if len(today) >= config.number("MAX_CALLS_PER_DAY", 15):
        raise CallError("limit_day")
    last4 = phone_e164[-4:]
    # Sheets stores "0123" as the number 123, so put the leading zeros back before comparing.
    if sum(1 for row in today if str(row.get("phone_last4", "")).zfill(4) == last4) >= CALLS_PER_NUMBER_PER_DAY:
        raise CallError("limit_number")


# ---------- what this call is about ----------

def memory_stories(stories: list[dict]) -> list[dict]:
    """Stories saved by the /test/story link are for checking the setup, not
    memories, so calls and the story book leave them out."""
    return [s for s in stories if s.get("source") != "test"]


def choose_plan(stories: list[dict], photos: list[dict], call_count: int) -> dict:
    """The one memory the next call is about: the newest photo no call has
    asked about yet, else his saved stories in turn (so repeat calls bring
    back different ones), else a topic he hasn't talked about yet."""
    pending = [p for p in photos if p.get("asked", "").upper() != "TRUE"]
    if pending:
        return {"kind": "photo", "photo": pending[-1]}
    if stories:
        return {"kind": "story", "story": stories[call_count % len(stories)]}
    topics = knowledge.new_topics() or ["the neighborhood where he grew up"]
    return {"kind": "topic", "topic": topics[0]}


def plan_summary(plan: dict) -> str:
    """One line saying what a call is about, for the calls tab and the page."""
    if plan["kind"] == "photo":
        description = plan["photo"].get("description", "").strip()
        short = description if len(description) <= 90 else description[:87].rstrip() + "..."
        return f"Asked about a photo: {short}"
    if plan["kind"] == "story":
        return f"Retold \"{plan['story'].get('title', '').strip()}\""
    return f"New topic: {plan['topic']}"


def build_context() -> dict:
    """Everything the call needs: weather, stories, a pending photo, and the plan.
    A Sheet or weather failure never stops a call; it just has less to say."""
    loaded = {}
    for tab in ("stories", "photos", "calls"):
        try:
            loaded[tab] = sheets.rows(tab)
        except Exception as error:
            log.error("Couldn't load %s for the call: %s", tab, error)
            loaded[tab] = []
    stories = memory_stories(loaded["stories"])
    plan = choose_plan(stories, loaded["photos"], len(loaded["calls"]))
    return {"weather": weather.current(), "stories": stories, "plan": plan}


def first_message(ctx: dict) -> str:
    name, builder = config.GRANDPA_NAME, config.BUILDER_NAME
    message = (
        f"Hi {name}, it's Storyline, the AI helper your grandson {builder} set up "
        f"to save your stories. This call goes into the family story book."
    )
    w = ctx.get("weather")
    if w:
        message += f" I hear it's {w['temp']} degrees with {w['description']} in {w['city']} today."
        if w["temp"] < 50:
            message += " Staying warm?"
        elif w["temp"] > 80:
            message += " Staying cool?"
        else:
            message += " How are you doing today?"
    else:
        message += " How are you doing today?"
    return message


def _plan_text(plan: dict, name: str) -> str:
    if plan["kind"] == "photo":
        p = plan["photo"]
        return (
            f"His family just sent you an old photo, and you have looked at it. After he "
            f"answers your greeting, tell him his family sent you a photo and you had a "
            f"look at it. Describe what you saw in a sentence or two, then ask this "
            f"question in your own words.\nWhat you saw in the photo: "
            f"{p.get('description', '')}\nThe question: {p.get('question', '')}"
        )
    if plan["kind"] == "story":
        s = plan["story"]
        return (
            f"Bring back a story {name} told you before. After he answers your greeting, "
            f"retell it in one or two warm sentences in your own words, then invite him to "
            f"add to it with one open question.\nThe story, \"{s.get('title', '')}\": "
            f"{s.get('summary', '')}"
        )
    return (
        f"Start a new story. After he answers your greeting, ask one open, gentle "
        f"question about: {plan['topic']}"
    )


def system_prompt(ctx: dict) -> str:
    name, builder = config.GRANDPA_NAME, config.BUILDER_NAME
    w = ctx.get("weather")
    weather_line = (
        f"It's {w['temp']}°F with {w['description']} in {w['city']} today."
        if w else "You don't know today's weather, so don't mention it."
    )
    story_lines = "\n".join(
        f"- {s.get('title', '')}: {s.get('summary', '')}" for s in ctx.get("stories", [])
    ) or "- (none saved yet)"

    return f"""You are Storyline, a warm, patient AI companion on a phone call with {name}. His grandson {builder} set you up to help {name} revisit and save his life stories. {name} is in his 80s and his memory is fading. Treat whoever answers as {name}.

## Your goal for this call: one memory
{_plan_text(ctx['plan'], name)}
Let the conversation follow him from there. If he brings up a different memory, follow it happily.

## How to talk with {name} (these rules matter most)
- Never quiz him. Never ask "Do you remember...?" or ask him to recall names, dates or details. Offer the memory instead, then invite him to add to it. For example: "You told me once about renting rowboats at Pine Lake for 50 cents an hour. What was that summer like?"
- If he can't recall something, warmly retell what he shared before and move on. Never make him feel he has failed.
- Never correct him or argue about details. If his version differs from what's written below, go with his version.
- Keep every turn short: one or two sentences, about 30 words at most. Then stop and let him answer. He should do most of the talking.
- Use simple words. Ask one question at a time. Leave room for pauses; if he's quiet, wait, then gently offer a small detail from his story to help him along.
- Before asking anything new, respond warmly and specifically to what he just said.
- If he seems confused or upset, slow down, reassure him, and move to a happier memory, or wrap up kindly.
- Speak about his late wife June warmly, and follow his lead.
- Give no medical advice. If he mentions pain, a fall or feeling unwell, respond with kindness and tell him you'll let his family know.
- This is a phone call: no lists, no emojis, no stage directions. Talk naturally.
- If he asks, be honest that you are an AI.

## Ending the call
Keep the call to about two and a half minutes. When it's time to wrap up, or the memory feels complete, thank him for sharing the story in one warm sentence, then use the endCall tool. The tool says goodbye for you, so don't say goodbye yourself.

## Today
{weather_line}

## About {name}
{knowledge.profile_for_prompt()}

## Stories {name} has told before
{story_lines}
"""


def build_assistant(ctx: dict) -> dict:
    name = config.GRANDPA_NAME
    base_url = config.get("PUBLIC_BASE_URL").rstrip("/")
    assistant = {
        "name": "Storyline",
        "firstMessage": first_message(ctx),
        "model": {
            "provider": config.get("VAPI_MODEL_PROVIDER", "anthropic"),
            "model": config.get("VAPI_MODEL", "claude-haiku-5-5"),
            "temperature": 0.7,
            "messages": [{"role": "system", "content": system_prompt(ctx)}],
            "tools": [{
                "type": "endCall",
                "messages": [{
                    "type": "request-start",
                    "content": f"Thank you, {name}. Talk to you soon. Goodbye!",
                }],
            }],
        },
        "voice": {
            "provider": config.get("VAPI_VOICE_PROVIDER", "vapi"),
            "voiceId": config.get("VAPI_VOICE_ID", "Leah"),
        },
        "maxDurationSeconds": MAX_CALL_SECONDS,
        "serverMessages": ["end-of-call-report"],
    }
    if base_url:
        assistant["server"] = {"url": f"{base_url}/webhooks/vapi"}
        if webhook_secret():
            assistant["server"]["headers"] = {WEBHOOK_HEADER: webhook_secret()}
    else:
        log.warning("PUBLIC_BASE_URL isn't set, so this call's story won't be saved")
    return assistant


# ---------- starting the call ----------

def start_call(phone_e164: str, assistant: dict) -> str:
    """Ask Vapi to dial the number. Returns Vapi's call id. Raises CallError."""
    api_key = config.get("VAPI_API_KEY")
    number_id = config.get("VAPI_PHONE_NUMBER_ID")
    if not (api_key and number_id):
        raise CallError("not_ready", "VAPI_API_KEY or VAPI_PHONE_NUMBER_ID is missing")
    base = config.get("VAPI_BASE_URL", "https://api.vapi.ai")
    try:
        response = requests.post(
            f"{base}/call",
            headers={"Authorization": f"Bearer {api_key}"},
            json={
                "phoneNumberId": number_id,
                "customer": {"number": phone_e164},
                "assistant": assistant,
            },
            timeout=TIMEOUT_SECONDS,
        )
    except requests.RequestException as error:
        raise CallError("call_failed", f"Couldn't reach Vapi: {error}") from None
    if response.status_code >= 400:
        raise CallError("call_failed", f"Vapi said {response.status_code}: {response.text[:500]}")
    call_id = (response.json() or {}).get("id", "")
    log.info("Call started: %s to ...%s", call_id or "(no id)", phone_e164[-4:])
    return call_id


def place_call(raw_phone: str) -> dict:
    """The whole POST /call flow after the demo code check. Raises CallError."""
    phone = normalize_us_phone(raw_phone)
    if not phone:
        raise CallError("bad_number")
    try:
        check_limits(phone)
    except CallError:
        raise
    except Exception as error:  # Sheet unreachable: don't block the call over it
        log.error("Couldn't check call limits: %s", error)

    ctx = build_context()
    call_id = start_call(phone, build_assistant(ctx))

    try:
        sheets.append("calls", {
            "timestamp": sheets.now_iso(),
            "phone_last4": phone[-4:],
            "status": "started",
            "call_id": call_id,
            "topic": plan_summary(ctx["plan"]),
        })
        if ctx["plan"]["kind"] == "photo":
            sheets.update_cell("photos", ctx["plan"]["photo"]["_row"], "asked", "TRUE")
    except Exception as error:
        log.error("Call started, but saving it to the Sheet failed: %s", error)
    return {"call_id": call_id, "plan": ctx["plan"]["kind"]}


# ---------- after the call (POST /webhooks/vapi) ----------

def _seconds_between(start: str, end: str) -> int | None:
    try:
        a = datetime.fromisoformat(start.replace("Z", "+00:00"))
        b = datetime.fromisoformat(end.replace("Z", "+00:00"))
    except (AttributeError, ValueError):
        return None
    return max(0, round((b - a).total_seconds()))


def call_duration(message: dict) -> int | None:
    """Seconds the call lasted, from whichever field Vapi provided."""
    for value in (message.get("durationSeconds"), (message.get("call") or {}).get("durationSeconds")):
        if isinstance(value, (int, float)):
            return round(value)
    call = message.get("call") or {}
    for source in (message, call):
        seconds = _seconds_between(source.get("startedAt"), source.get("endedAt"))
        if seconds is not None:
            return seconds
    return None


def call_transcript(message: dict) -> str:
    """The transcript text. Vapi puts it at message.artifact.transcript; fall
    back to building it from artifact.messages (role + message)."""
    artifact = message.get("artifact") or {}
    text = artifact.get("transcript") or message.get("transcript") or ""
    if text.strip():
        return text.strip()
    lines = []
    for item in artifact.get("messages") or []:
        role, said = item.get("role", ""), (item.get("message") or "").strip()
        if not said or role in ("system", "tool_calls", "tool_call_result"):
            continue
        lines.append(f"{'User' if role == 'user' else 'AI'}: {said}")
    return "\n".join(lines)


def _update_call_row(call_id: str, duration: int | None, ended_reason: str) -> None:
    for row in sheets.rows("calls"):
        if row.get("call_id") == call_id:
            sheets.update_cell("calls", row["_row"], "status", "ended")
            sheets.update_cell("calls", row["_row"], "duration_seconds", "" if duration is None else duration)
            sheets.update_cell("calls", row["_row"], "ended_reason", ended_reason)
            return
    log.warning("No calls row for call %s (a replay, or the row wasn't saved)", call_id)


def handle_end_of_call(message: dict, source: str = "call") -> dict | None:
    """Save the story from a finished call. Runs in a background thread.
    Returns the saved story, or None when there was nothing to save.
    source is "call" for real calls and "test" for the /test/story link."""
    call_id = (message.get("call") or {}).get("id") or ""
    duration = call_duration(message)
    ended_reason = message.get("endedReason") or ""
    log.info("Call ended: %s after %ss (%s)", call_id or "(no id)", duration, ended_reason)

    try:
        _update_call_row(call_id, duration, ended_reason)
    except Exception as error:
        log.error("Couldn't update the calls row for %s: %s", call_id, error)

    transcript = call_transcript(message)
    if duration is not None and duration < MIN_STORY_SECONDS:
        log.info("No story saved for %s: the call was only %ss", call_id, duration)
        return None
    if "User:" not in transcript:
        artifact_keys = sorted((message.get("artifact") or {}).keys())
        log.info("No story saved for %s: no words from Frank in the transcript (artifact has %s)",
                 call_id, artifact_keys)
        return None

    try:
        if call_id and any(s.get("call_id") == call_id for s in sheets.rows("stories")):
            log.info("Story for %s is already saved (Vapi sent the report twice)", call_id)
            return None
    except Exception as error:
        log.error("Couldn't check for an existing story: %s", error)

    story = claude_ai.summarize_call(transcript)
    if not story:
        return None
    if not story.get("has_story"):
        log.info("No story saved for %s: Claude found no memory in the call", call_id)
        return None

    record = {
        "timestamp": sheets.now_iso(),
        "title": story.get("title", "").strip(),
        "summary": story.get("summary", "").strip(),
        "people": story.get("people", "").strip(),
        "places": story.get("places", "").strip(),
        "concern": story.get("concern", "").strip(),
        "source": source,
        "call_id": call_id,
    }
    try:
        sheets.append("stories", record)
    except Exception as error:
        log.error("Story '%s' couldn't be saved to the Sheet: %s", record["title"], error)
        return None
    log.info("Story saved: %s%s", record["title"], " (with a concern for the family)" if record["concern"] else "")
    try:
        texting.send_family_story(record)
    except Exception as error:
        log.error("Story saved, but the family text failed: %s", error)
    return record

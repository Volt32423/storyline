"""Build the call prompt and start the Vapi outbound call (the Voice superpower).

Field names checked against docs.vapi.ai (Nov 2026):
- POST https://api.vapi.ai/call with phoneNumberId, customer.number and an
  inline (transient) `assistant`
- assistant: name, firstMessage, model {provider, model, messages, tools},
  voice {provider, voiceId}, maxDurationSeconds, server {url}, serverMessages
- hang-up tool: model.tools [{type: "endCall", messages: [{type: "request-start", content}]}]
- provider "anthropic" offers "claude-haiku-5-5"; provider "vapi" offers voice "Leah"
"""

import logging
import re
from datetime import datetime
from zoneinfo import ZoneInfo

import requests

from services import config, knowledge, sheets, weather

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 20
MAX_CALL_SECONDS = 180
CALLS_PER_NUMBER_PER_DAY = 3


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
    if sum(1 for row in today if row.get("phone_last4") == last4) >= CALLS_PER_NUMBER_PER_DAY:
        raise CallError("limit_number")


# ---------- what this call is about ----------

def build_context() -> dict:
    """Everything the call needs: weather, stories, a pending photo, and the plan.
    A Sheet or weather failure never stops a call; it just has less to say."""
    try:
        stories = sheets.rows("stories")
    except Exception as error:
        log.error("Couldn't load stories for the call: %s", error)
        stories = []
    try:
        pending = [p for p in sheets.rows("photos") if p.get("asked", "").upper() != "TRUE"]
    except Exception as error:
        log.error("Couldn't load photos for the call: %s", error)
        pending = []
    photo = pending[-1] if pending else None

    if photo:
        plan = {"kind": "photo", "photo": photo}
    elif stories:
        # Rotate through his stories so repeat calls bring back different ones.
        try:
            call_count = len(sheets.rows("calls"))
        except Exception:
            call_count = 0
        plan = {"kind": "story", "story": stories[call_count % len(stories)]}
    else:
        topics = knowledge.new_topics() or ["the neighborhood where he grew up"]
        plan = {"kind": "topic", "topic": topics[0]}

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
            f"A family member just sent an old photo. After he answers your greeting, "
            f"describe the photo to him in a sentence or two, then ask this question in "
            f"your own words.\nThe photo: {p.get('description', '')}\n"
            f"The question: {p.get('question', '')}"
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
- Use short, simple sentences. Ask one question at a time. Leave room for pauses and let him talk.
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
        })
        if ctx["plan"]["kind"] == "photo":
            sheets.update_cell("photos", ctx["plan"]["photo"]["_row"], "asked", "TRUE")
    except Exception as error:
        log.error("Call started, but saving it to the Sheet failed: %s", error)
    return {"call_id": call_id, "plan": ctx["plan"]["kind"]}

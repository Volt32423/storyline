"""Old family photos: from a Linq text or the upload form, to a description and
question in the Sheet that the next call asks about."""

import logging
import threading
from collections import OrderedDict

from services import claude_ai, config, sheets, texting

log = logging.getLogger(__name__)

STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT"}  # never answered

_seen_events: "OrderedDict[str, None]" = OrderedDict()
_seen_lock = threading.Lock()


def save_photo(raw: bytes, source: str) -> dict | None:
    """Describe a photo with Claude and add it to the photos tab.
    Returns the saved row, or None if it couldn't be used."""
    result = claude_ai.describe_photo(raw)
    if not result:
        return None
    if not result.get("suitable", True):
        log.info("Photo not saved: Claude marked it unsuitable for a family call")
        return None
    record = {
        "timestamp": sheets.now_iso(),
        "source": source,
        "description": result.get("description", "").strip(),
        "question": result.get("question", "").strip(),
        "asked": "FALSE",
    }
    sheets.append("photos", record)
    log.info("Photo described and saved (%s): %s", source, record["description"][:80])
    return record


def first_time(event_id: str) -> bool:
    """True the first time an event id is seen (Linq may deliver twice)."""
    if not event_id:
        return True
    with _seen_lock:
        if event_id in _seen_events:
            return False
        _seen_events[event_id] = None
        while len(_seen_events) > 500:
            _seen_events.popitem(last=False)
        return True


def _message_fields(event: dict) -> tuple[str, list, str, bool]:
    """(chat_id, parts, sender, from_us) for both the 2026-02-03 payload and
    the older 2025-01-01 one."""
    data = event.get("data") or {}
    if "parts" in data or "direction" in data:  # 2026-02-03
        chat = data.get("chat") or {}
        sender = data.get("sender_handle") or {}
        from_us = data.get("direction") == "outbound" or bool(sender.get("is_me"))
        return chat.get("id", ""), data.get("parts") or [], sender.get("handle", ""), from_us
    message = data.get("message") or {}  # 2025-01-01
    return (data.get("chat_id", ""), message.get("parts") or [], data.get("from", ""),
            bool(data.get("is_from_me")))


def handle_linq_event(event: dict) -> int:
    """Process one Linq webhook event. Returns how many photos were saved."""
    if event.get("event_type") != "message.received":
        return 0
    chat_id, parts, sender, from_us = _message_fields(event)
    if from_us:
        return 0
    name = config.GRANDPA_NAME
    words = " ".join(str(p.get("value", "")) for p in parts if p.get("type") == "text").strip()
    if words.upper().strip(".! ") in STOP_WORDS:
        log.info("Opt-out text from ...%s: not replying", sender[-4:])
        return 0
    media = [p for p in parts if p.get("type") == "media" and p.get("url")]
    if not media:
        log.info("Text received from ...%s with no photo", sender[-4:])
        texting.reply_in_chat(chat_id, f"Hi! Text an old family photo to this number, and Storyline "
                                       f"will ask Grandpa {name} about it on his next call.")
        return 0

    saved = 0
    for part in media:
        try:
            raw = texting.download(part["url"])
        except Exception as error:
            log.error("Couldn't download a texted photo: %s", error)
            continue
        try:
            if save_photo(raw, source="text"):
                saved += 1
        except Exception as error:
            log.error("Couldn't save a texted photo: %s", error)
    if saved:
        texting.reply_in_chat(chat_id, f"Got it! Storyline will ask Grandpa {name} about this photo on the next call.")
    else:
        texting.reply_in_chat(chat_id, "Sorry, I couldn't use that photo. Could you try sending a different one?")
    return saved

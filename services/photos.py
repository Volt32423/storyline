"""Old family photos: from a text (Sendblue or Linq) or the upload form, to a
description and question in the Sheet that the next call asks about."""

import logging
import threading
import time
from collections import OrderedDict

from services import claude_ai, config, linq, sendblue, sheets

log = logging.getLogger(__name__)

# Never answered. The texting service handles the opt-out itself.
STOP_WORDS = {"STOP", "STOPALL", "UNSUBSCRIBE", "CANCEL", "END", "QUIT", "OPT OUT", "OPTOUT", "REVOKE"}

# Sendblue may deliver a photo and its caption as two separate messages. Wait a
# moment before sending the "text me a photo" tip, and skip it if a photo came.
HINT_DELAY_SECONDS = 8
PHOTO_GRACE_SECONDS = 120
HINT_EVERY_SECONDS = 3600
_last_photo: dict[str, float] = {}
_last_hint: dict[str, float] = {}

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
    """True the first time an event id is seen (Linq and Sendblue may deliver twice)."""
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


def _is_opt_out(words: str) -> bool:
    return words.upper().strip(".! ") in STOP_WORDS


def _hint() -> str:
    return (f"Hi! Text an old family photo to this number, and Storyline "
            f"will ask Grandpa {config.GRANDPA_NAME} about it on his next call.")


def _got_it() -> str:
    return f"Got it! Storyline will ask Grandpa {config.GRANDPA_NAME} about this photo on the next call."


SORRY = "Sorry, I couldn't use that photo. Could you try sending a different one?"


def _save_texted(urls: list[str], download) -> int:
    """Download and save each texted photo. Returns how many were saved."""
    saved = 0
    for url in urls:
        try:
            raw = download(url)
        except Exception as error:
            log.error("Couldn't download a texted photo: %s", error)
            continue
        try:
            if save_photo(raw, source="text"):
                saved += 1
        except Exception as error:
            log.error("Couldn't save a texted photo: %s", error)
    return saved


def handle_linq_event(event: dict) -> int:
    """Process one Linq webhook event. Returns how many photos were saved."""
    if event.get("event_type") != "message.received":
        return 0
    chat_id, parts, sender, from_us = _message_fields(event)
    if from_us:
        return 0
    words = " ".join(str(p.get("value", "")) for p in parts if p.get("type") == "text").strip()
    if _is_opt_out(words):
        log.info("Opt-out text from ...%s: not replying", sender[-4:])
        return 0
    media = [p["url"] for p in parts if p.get("type") == "media" and p.get("url")]
    if not media:
        log.info("Text received from ...%s with no photo", sender[-4:])
        linq.reply_in_chat(chat_id, _hint())
        return 0
    saved = _save_texted(media, linq.download)
    linq.reply_in_chat(chat_id, _got_it() if saved else SORRY)
    return saved


def handle_sendblue_message(message: dict) -> int:
    """Process one incoming Sendblue message (the flat JSON its receive
    webhook posts). Returns how many photos were saved."""
    if message.get("is_outbound"):
        return 0
    sender = str(message.get("number") or message.get("from_number") or "")
    if message.get("group_id"):
        log.info("Group text from ...%s ignored: Storyline only works in one-to-one texts", sender[-4:])
        return 0
    words = str(message.get("content") or "").strip()
    if _is_opt_out(words):
        log.info("Opt-out text from ...%s: not replying", sender[-4:])
        return 0

    def reply(text: str) -> None:
        if message.get("opted_out") or not sender.startswith("+"):
            return
        sendblue.send(sender, text, from_number=str(message.get("sendblue_number") or ""))

    url = str(message.get("media_url") or "").strip()
    if not url:
        if not words:
            log.info("Text from ...%s had no words and no media_url (fields: %s)",
                     sender[-4:], ", ".join(sorted(message)))
            return 0
        log.info("Text received from ...%s with no photo", sender[-4:])
        time.sleep(HINT_DELAY_SECONDS)
        now = time.monotonic()
        if (now - _last_photo.get(sender, -1e9) > PHOTO_GRACE_SECONDS
                and now - _last_hint.get(sender, -1e9) > HINT_EVERY_SECONDS):
            _last_hint[sender] = now
            reply(_hint())
        return 0
    _last_photo[sender] = time.monotonic()
    saved = _save_texted([url], sendblue.download)
    reply(_got_it() if saved else SORRY)
    return saved

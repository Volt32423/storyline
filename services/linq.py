"""Linq: send texts, and check that incoming webhooks really came from Linq.
One of the two texting services; services/texting.py picks which one is on.

Checked against docs.linqapp.com (Nov 2026):
- API base https://api.linqapp.com/api/partner, header Authorization: Bearer <token>
- New message:   POST /v3/messages  {"to": ["+1..."], "message": {"parts": [{"type": "text", "value": "..."}]}}
- Reply in chat: POST /v3/chats/{chatId}/messages  {"message": {"parts": [...]}}
- Webhooks use the Standard Webhooks scheme: headers webhook-id, webhook-timestamp,
  webhook-signature ("v1,<base64>"), HMAC-SHA256 over "{id}.{timestamp}.{raw body}",
  key = base64-decoded secret after its "whsec_" prefix, reject if over 5 minutes old.
"""

import base64
import hashlib
import hmac
import logging
import time

import requests

from services import config

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15
MAX_WEBHOOK_AGE_SECONDS = 300


def _base() -> str:
    return config.get("LINQ_BASE_URL", "https://api.linqapp.com").rstrip("/") + "/api/partner"


def _headers() -> dict:
    return {"Authorization": f"Bearer {config.require('LINQ_API_TOKEN')}"}


def is_configured() -> bool:
    return bool(config.get("LINQ_API_TOKEN"))


def sending_enabled() -> bool:
    return config.flag("SMS_SENDING_ENABLED", True) and is_configured()


def _text_body(text: str) -> dict:
    return {"message": {"parts": [{"type": "text", "value": text}]}}


def send_to_number(number: str, text: str) -> bool:
    """Text a phone number (E.164). Returns True if Linq accepted it. Never raises."""
    if not sending_enabled():
        return False
    try:
        response = requests.post(f"{_base()}/v3/messages", headers=_headers(),
                                 json={"to": [number], **_text_body(text)}, timeout=TIMEOUT_SECONDS)
        if response.status_code >= 400:
            log.error("Text to ...%s failed: Linq said %s: %s", number[-4:], response.status_code, response.text[:300])
            return False
    except Exception as error:
        log.error("Text to ...%s failed: %s", number[-4:], error)
        return False
    log.info("Text sent to ...%s", number[-4:])
    return True


def reply_in_chat(chat_id: str, text: str) -> bool:
    """Reply inside an existing Linq chat. Returns True if accepted. Never raises."""
    if not (sending_enabled() and chat_id):
        return False
    try:
        response = requests.post(f"{_base()}/v3/chats/{chat_id}/messages", headers=_headers(),
                                 json=_text_body(text), timeout=TIMEOUT_SECONDS)
        if response.status_code >= 400:
            log.error("Reply in chat failed: Linq said %s: %s", response.status_code, response.text[:300])
            return False
    except Exception as error:
        log.error("Reply in chat failed: %s", error)
        return False
    return True


def download(url: str) -> bytes:
    """Fetch an attachment from Linq's CDN. Tries without the token first
    (the docs don't say the CDN needs it), then with it."""
    response = requests.get(url, timeout=30)
    if response.status_code in (401, 403) and is_configured():
        response = requests.get(url, headers=_headers(), timeout=30)
    response.raise_for_status()
    return response.content


def verify_webhook(raw_body: bytes, headers) -> bool:
    """True if the request is signed with LINQ_WEBHOOK_SECRET and recent."""
    secret = config.get("LINQ_WEBHOOK_SECRET")
    if not secret:
        log.warning("Rejected a Linq webhook: LINQ_WEBHOOK_SECRET isn't set")
        return False
    msg_id = headers.get("webhook-id", "")
    timestamp = headers.get("webhook-timestamp", "")
    signatures = headers.get("webhook-signature", "")
    if not (msg_id and timestamp and signatures):
        return False
    try:
        if abs(time.time() - int(timestamp)) > MAX_WEBHOOK_AGE_SECONDS:
            log.warning("Rejected a Linq webhook: timestamp is too old")
            return False
        key = base64.b64decode(secret.removeprefix("whsec_"))
    except (ValueError, TypeError):
        return False
    signed = f"{msg_id}.{timestamp}.".encode() + raw_body
    expected = base64.b64encode(hmac.new(key, signed, hashlib.sha256).digest()).decode()
    for candidate in signatures.split():
        version, _, value = candidate.partition(",")
        if version == "v1" and hmac.compare_digest(value, expected):
            return True
    return False


def create_webhook_subscription(target_url: str) -> dict:
    """Subscribe target_url to message.received. Returns Linq's reply, which
    includes the signing secret. Raises on failure."""
    response = requests.post(
        f"{_base()}/v3/webhook-subscriptions",
        headers=_headers(),
        json={"target_url": target_url, "subscribed_events": ["message.received"]},
        timeout=TIMEOUT_SECONDS,
    )
    if response.status_code >= 400:
        raise RuntimeError(f"Linq said {response.status_code}: {response.text[:500]}")
    return response.json()

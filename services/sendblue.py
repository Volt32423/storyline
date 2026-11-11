"""Sendblue: iMessage texting. One of the two texting services;
services/texting.py picks which one is on.

Checked against docs.sendblue.com (Nov 2026):
- API base https://api.sendblue.com, headers sb-api-key-id and sb-api-secret-key.
- Send: POST /api/send-message {"number": "+1...", "from_number": "+1...", "content": "..."}.
  from_number (our Sendblue number) is required on the free shared-number plan. A reply
  can be HTTP 200 and still say status "ERROR" with an error_code, so both are checked.
- Receive: Sendblue POSTs each incoming message to the account's "receive" webhooks as flat
  JSON: number (who sent it), content, media_url (one attachment; the link lasts 30 days),
  message_handle, is_outbound, group_id, opted_out, sendblue_number and more. Answer with
  a 2xx or it retries (up to 3 times on a 5xx). Never answer 410: that deletes the webhook.
- Webhook secret: set per webhook; Sendblue sends it back in the sb-signing-secret header.
- Webhooks: GET (list), POST (append) and DELETE /api/account/webhooks, with
  {"webhooks": [{"url": ..., "secret": ...}], "type": "receive"}.
- Free plan: one number shared with other free users, and up to 10 verified contacts. A
  person's texts only reach this app, and we can only text them, once they've been added
  as a contact and have texted the number once.
"""

import hashlib
import hmac
import logging

import requests

from services import config

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 15
SECRET_HEADER = "sb-signing-secret"
MAX_PHOTO_BYTES = 25 * 1024 * 1024
FREE_PLAN_HINT = ("On Sendblue's free plan, the number must be a verified contact "
                  "that has texted the Sendblue number once.")


def _base() -> str:
    return config.get("SENDBLUE_BASE_URL", "https://api.sendblue.com").rstrip("/")


def _headers() -> dict:
    return {"sb-api-key-id": config.require("SENDBLUE_API_KEY"),
            "sb-api-secret-key": config.require("SENDBLUE_API_SECRET")}


def is_configured() -> bool:
    return all(config.get(name) for name in ("SENDBLUE_API_KEY", "SENDBLUE_API_SECRET", "SENDBLUE_PHONE_NUMBER"))


def sending_enabled() -> bool:
    return config.flag("SMS_SENDING_ENABLED", True) and is_configured()


def _json(response) -> dict:
    try:
        body = response.json()
    except ValueError:
        return {}
    return body if isinstance(body, dict) else {}


def _failed(response, body: dict) -> bool:
    return (response.status_code >= 400 or str(body.get("status", "")).upper() == "ERROR"
            or body.get("error_code") not in (None, 0, "0", ""))


def _why(response, body: dict) -> str:
    detail = body.get("error_key") or body.get("error_message") or body.get("message") or response.text
    return f"{response.status_code} {str(detail)[:300]}"


def send(number: str, text: str, from_number: str = "") -> bool:
    """Text a phone number (E.164) from our Sendblue number. Returns True if
    Sendblue accepted it. Never raises."""
    if not sending_enabled():
        return False
    body = {"number": number, "from_number": from_number or config.get("SENDBLUE_PHONE_NUMBER"), "content": text}
    try:
        response = requests.post(f"{_base()}/api/send-message", headers=_headers(), json=body,
                                 timeout=TIMEOUT_SECONDS)
        reply = _json(response)
        if _failed(response, reply):
            log.error("Text to ...%s failed: Sendblue said %s. %s", number[-4:], _why(response, reply), FREE_PLAN_HINT)
            return False
    except Exception as error:
        log.error("Text to ...%s failed: %s", number[-4:], error)
        return False
    log.info("Text sent to ...%s", number[-4:])
    return True


def _get_capped(url: str, headers: dict | None = None):
    response = requests.get(url, headers=headers, timeout=30, stream=True)
    if response.status_code >= 400:
        return response, b""
    data = bytearray()
    for chunk in response.iter_content(64 * 1024):
        data += chunk
        if len(data) > MAX_PHOTO_BYTES:
            response.close()
            raise ValueError(f"the file is over {MAX_PHOTO_BYTES // (1024 * 1024)} MB")
    return response, bytes(data)


def download(url: str) -> bytes:
    """Fetch a texted photo from its media_url. Tries without the API keys
    first (the docs call it a CDN link), then with them."""
    response, data = _get_capped(url)
    if response.status_code in (401, 403) and is_configured():
        response, data = _get_capped(url, _headers())
    response.raise_for_status()
    return data


# ---------- the webhook that brings incoming texts here ----------

def webhook_secret() -> str:
    """The secret Sendblue sends back with every incoming text, so only
    Sendblue can post to /webhooks/sendblue. Derived from SHEETS_WEBAPP_SECRET,
    so there's nothing extra to set up; SENDBLUE_WEBHOOK_SECRET overrides it."""
    explicit = config.get("SENDBLUE_WEBHOOK_SECRET")
    if explicit:
        return explicit
    base = config.get("SHEETS_WEBAPP_SECRET")
    if not base:
        return ""
    return hmac.new(base.encode(), b"storyline-sendblue-webhook", hashlib.sha256).hexdigest()[:40]


def webhook_url() -> str:
    """Where Sendblue posts incoming texts. The secret rides along in the URL
    too, so the webhook is checked even if the header never arrives."""
    base_url = config.get("PUBLIC_BASE_URL").rstrip("/")
    secret = webhook_secret()
    return f"{base_url}/webhooks/sendblue?key={secret}" if base_url and secret else ""


def verify_webhook(headers, args) -> bool:
    """True if the request carries our secret, in the sb-signing-secret header
    or the URL's key."""
    expected = webhook_secret().encode()
    if not expected:
        log.warning("Rejected a Sendblue webhook: no secret is set (SHEETS_WEBAPP_SECRET or SENDBLUE_WEBHOOK_SECRET)")
        return False
    for given in (headers.get(SECRET_HEADER, ""), args.get("key", "")):
        if given and hmac.compare_digest(given.encode(), expected):
            return True
    return False


def _call(method: str, path: str, body: dict | None = None) -> dict:
    response = requests.request(method, f"{_base()}{path}", headers=_headers(), json=body, timeout=TIMEOUT_SECONDS)
    reply = _json(response)
    if response.status_code >= 400 or str(reply.get("status", "")).upper() == "ERROR":
        raise RuntimeError(f"Sendblue said {_why(response, reply)}")
    return reply


def _receive_urls(reply: dict) -> list[str]:
    hooks = reply.get("webhooks") or {}
    items = hooks.get("receive") if isinstance(hooks, dict) else hooks
    urls = []
    for item in items or []:
        if isinstance(item, str):
            urls.append(item)
        elif isinstance(item, dict) and item.get("url"):
            urls.append(str(item["url"]))
    return urls


def set_up_webhook() -> str:
    """Make sure Sendblue posts incoming texts to this app, with our secret.
    Leaves the account's other webhooks alone. Returns "already", "added" or
    "replaced" (an older Storyline webhook was swapped out). Raises on failure."""
    target = webhook_url()
    if not (is_configured() and target):
        raise RuntimeError("Sendblue or PUBLIC_BASE_URL isn't set up")
    ours = target.split("?", 1)[0]
    urls = _receive_urls(_call("GET", "/api/account/webhooks"))
    if target in urls:
        return "already"
    old = [url for url in urls if url.split("?", 1)[0].rstrip("/") == ours]
    if old:
        _call("DELETE", "/api/account/webhooks", {"webhooks": old, "type": "receive"})
    _call("POST", "/api/account/webhooks",
          {"webhooks": [{"url": target, "secret": webhook_secret()}], "type": "receive"})
    return "replaced" if old else "added"

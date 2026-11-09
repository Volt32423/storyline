"""Storyline: Flask routes only. The real work lives in services/."""

import hashlib
import hmac
import json
import logging
import tempfile
import threading
import time
from pathlib import Path

import requests
from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from services import calls, claude_ai, config, knowledge, page, photos, sheets, texting

try:
    import fcntl  # Linux/macOS only; Render runs Linux
except ImportError:  # pragma: no cover (Windows)
    fcntl = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("storyline")
for noisy in ("httpx", "httpx2", "httpcore"):  # one line per HTTP request is too chatty
    logging.getLogger(noisy).setLevel(logging.WARNING)

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 20 * 1024 * 1024  # uploads bigger than 20 MB are refused
app.jinja_env.filters["curly"] = page.curly  # it's -> it’s on the page

# Changes whenever the stylesheet does, so browsers never pair a new page with an old one.
CSS_VERSION = hashlib.sha256((config.ROOT / "static" / "style.css").read_bytes()).hexdigest()[:10]

SAMPLE_CALL = config.ROOT / "tests" / "fixtures" / "end_of_call_report.json"
LINQ_WEBHOOK_VERSION = "2026-02-03"  # the payload shape services/photos.py reads


SETUP_TRIES = 30
SETUP_RETRY_SECONDS = 60


def _set_up_sheet() -> None:
    """On startup, create the Sheet's tabs and seed stories if they're missing.
    If the Sheet can't be reached, retry every minute for half an hour, so
    fixing the Apps Script deployment takes effect without a redeploy.
    A file lock keeps two gunicorn workers from seeding at the same moment."""
    if not sheets.is_configured():
        log.warning("Sheet not set up yet: add SHEETS_WEBAPP_URL and SHEETS_WEBAPP_SECRET")
        return
    lock_path = Path(tempfile.gettempdir()) / "storyline-sheet-setup.lock"
    for attempt in range(1, SETUP_TRIES + 1):
        try:
            with open(lock_path, "w") as lock:
                if fcntl:
                    fcntl.flock(lock, fcntl.LOCK_EX)
                added = knowledge.seed_sheet()
            log.info("Sheet ready (%d seed stories added)", added)
            return
        except Exception as error:
            log.error(
                "Sheet setup failed (try %d of %d): %s. Check SHEETS_WEBAPP_URL, "
                "SHEETS_WEBAPP_SECRET and the script's deployment. Retrying in %d seconds.",
                attempt, SETUP_TRIES, error, SETUP_RETRY_SECONDS,
            )
            time.sleep(SETUP_RETRY_SECONDS)
    log.error("Sheet setup gave up after %d tries. Fix the problem above, then redeploy.", SETUP_TRIES)


threading.Thread(target=_set_up_sheet, name="sheet-setup", daemon=True).start()


KEEP_AWAKE_MINUTES = 10  # Render's free plan sleeps after 15 minutes without visitors


def _keep_awake() -> None:
    """Visit our own public /health every few minutes, so Render's free plan
    never puts the app to sleep and no judge waits a minute for it to wake.
    The request goes out and back in through Render's front door, so it
    counts as a visitor. Off when PUBLIC_BASE_URL isn't set, or when
    KEEP_AWAKE_MINUTES is 0."""
    base = config.get("PUBLIC_BASE_URL").rstrip("/")
    try:
        minutes = float(config.get("KEEP_AWAKE_MINUTES", str(KEEP_AWAKE_MINUTES)))
    except ValueError:
        minutes = KEEP_AWAKE_MINUTES
    if not base or minutes <= 0:
        log.info("Keep-awake is off (set PUBLIC_BASE_URL to turn it on)")
        return
    log.info("Keep-awake on: visiting %s/health every %g minutes", base, minutes)
    failures = 0
    while True:
        time.sleep(minutes * 60)
        try:
            response = requests.get(f"{base}/health", timeout=30)
            response.raise_for_status()
            if failures:
                log.info("Keep-awake works again after %d failed visits", failures)
            failures = 0
        except Exception as error:
            failures += 1
            log.warning("Keep-awake visit failed (%d in a row): %s", failures, error)


threading.Thread(target=_keep_awake, name="keep-awake", daemon=True).start()


MESSAGES = {
    "calling": ("ok", "Calling you now. Pick up and say hello! After you hang up, your story "
                      "appears in the story book below within a minute or two. Refresh to see it."),
    "bad_code": ("error", "That demo code isn't right. It's in our project submission."),
    "bad_number": ("error", "Please enter a 10-digit US phone number."),
    "limit_number": ("error", "That number has had 3 calls today. Please try again tomorrow."),
    "limit_day": ("error", "Storyline has made all its calls for today. Please try again tomorrow."),
    "not_ready": ("error", "Calls aren't switched on yet. Please try again later."),
    "call_failed": ("error", "Sorry, the call couldn't be started. Please try again in a minute."),
    "photo_ok": ("ok", "Got it! Claude looked at your photo. What it saw is below, and the next "
                       "call asks Grandpa Frank about it."),
    "photo_bad": ("error", "Please choose a photo file (under 20 MB)."),
    "photo_bad_code": ("error", "That demo code isn't right. It's in our project submission."),
    "photo_failed": ("error", "Sorry, that photo couldn't be used. Please try a different one."),
}


def _code_ok(code: str) -> bool:
    expected = config.get("DEMO_CODE")
    return bool(expected) and hmac.compare_digest(code.strip().encode(), expected.encode())


@app.get("/")
def index():
    msg = request.args.get("msg", "")
    status = MESSAGES.get(msg)
    return render_template(
        "index.html",
        page=page.page_data(),
        css_version=CSS_VERSION,
        call_status=status if status and not msg.startswith("photo") else None,
        photo_status=status if status and msg.startswith("photo") else None,
    )


@app.get("/book")
def book():
    """Frank's story book as a keepsake: every story, oldest first, ready to
    print or save as a PDF from the browser."""
    return render_template("book.html", book=page.book_data(), css_version=CSS_VERSION)


@app.post("/call")
def call():
    if not config.get("DEMO_CODE"):
        return redirect(url_for("index", msg="not_ready", _anchor="try"))
    if not _code_ok(request.form.get("code", "")):
        return redirect(url_for("index", msg="bad_code", _anchor="try"))
    try:
        calls.place_call(request.form.get("phone", ""))
    except calls.CallError as error:
        if error.code in ("call_failed", "not_ready"):
            log.error("Call not started: %s", error)
        return redirect(url_for("index", msg=error.code, _anchor="try"))
    except Exception:
        log.exception("Call not started: unexpected error")
        return redirect(url_for("index", msg="call_failed", _anchor="try"))
    return redirect(url_for("index", msg="calling", _anchor="try"))


@app.get("/call/preview")
def call_preview():
    """What the next call would say, without dialing. Needs the demo code:
    /call/preview?code=..."""
    if not _code_ok(request.args.get("code", "")):
        abort(403)
    ctx = calls.build_context()
    assistant = calls.build_assistant(ctx)
    return jsonify({
        "plan": ctx["plan"]["kind"],
        "weather": ctx["weather"],
        "first_message": assistant["firstMessage"],
        "system_prompt": assistant["model"]["messages"][0]["content"],
        "model": assistant["model"]["model"],
        "voice": assistant["voice"],
    })


@app.get("/test/story")
def test_story():
    """Check the story step without a phone call: runs a sample call transcript
    through Claude and shows the result. Add &save=1 to also save it to the
    Sheet (as source "test", which calls never bring up). Needs the demo code."""
    if not _code_ok(request.args.get("code", "")):
        abort(403)
    message = json.loads(SAMPLE_CALL.read_text(encoding="utf-8"))["message"]
    if request.args.get("save") == "1":
        message["call"]["id"] = f"test-{int(time.time() * 1000)}"
        story = calls.handle_end_of_call(message, source="test")
        if not story:
            return jsonify({"ok": False, "error": "Nothing was saved. Check Render's logs for the reason."}), 500
        return jsonify({"ok": True, "saved_to_sheet": True, "story": story})
    story = claude_ai.summarize_call(calls.call_transcript(message))
    if not story:
        return jsonify({"ok": False, "error": "Claude didn't answer. Check ANTHROPIC_API_KEY and Render's logs."}), 500
    return jsonify({"ok": True, "saved_to_sheet": False, "story": story})


@app.post("/upload")
def upload():
    """Backup path for the photo flow: describe an uploaded photo and save it,
    exactly like a texted one. Needs the demo code."""
    if not _code_ok(request.form.get("code", "")):
        return redirect(url_for("index", msg="photo_bad_code", _anchor="photos"))
    file = request.files.get("photo")
    raw = file.read() if file else b""
    if not raw:
        return redirect(url_for("index", msg="photo_bad", _anchor="photos"))
    try:
        saved = photos.save_photo(raw, source="upload")
    except Exception:
        log.exception("Uploaded photo couldn't be saved")
        saved = None
    return redirect(url_for("index", msg="photo_ok" if saved else "photo_failed", _anchor="photos"))


@app.errorhandler(413)
def too_large(_error):
    return redirect(url_for("index", msg="photo_bad", _anchor="photos"))


@app.post("/webhooks/linq")
def linq_webhook():
    """Linq posts here when someone texts Storyline's number. Check the
    signature, answer right away, and handle the photo in the background."""
    raw = request.get_data()
    if not texting.verify_webhook(raw, request.headers):
        log.warning("Rejected a Linq webhook with a missing or bad signature")
        return jsonify({"error": "invalid signature"}), 401
    try:
        event = json.loads(raw)
    except ValueError:
        return jsonify({"error": "bad json"}), 400
    if event.get("event_type") == "message.received" and photos.first_time(event.get("event_id", "")):
        threading.Thread(target=_safe_linq_event, args=(event,), daemon=True).start()
    return jsonify({"ok": True})


def _safe_linq_event(event: dict) -> None:
    try:
        photos.handle_linq_event(event)
    except Exception:
        log.exception("Handling a Linq message failed")


@app.get("/setup/linq")
def setup_linq():
    """One-time setup: subscribe this app to Linq's message.received webhook and
    show the signing secret to copy into LINQ_WEBHOOK_SECRET. Needs the demo
    code, and switches itself off once LINQ_WEBHOOK_SECRET is set."""
    if not _code_ok(request.args.get("code", "")):
        abort(403)
    if config.get("LINQ_WEBHOOK_SECRET"):
        return jsonify({"ok": False, "error": "Already set up: LINQ_WEBHOOK_SECRET is set."}), 409
    base_url = config.get("PUBLIC_BASE_URL").rstrip("/")
    if not (texting.is_configured() and base_url):
        return jsonify({"ok": False, "error": "Set LINQ_API_TOKEN and PUBLIC_BASE_URL in Render first."}), 400
    target = f"{base_url}/webhooks/linq?version={LINQ_WEBHOOK_VERSION}"
    try:
        reply = texting.create_webhook_subscription(target)
    except Exception as error:
        log.error("Linq webhook setup failed: %s", error)
        return jsonify({"ok": False, "error": str(error)}), 502
    log.info("Linq webhook subscription created for %s", target)
    return jsonify({
        "ok": True,
        "next_step": "Copy the signing secret below (it starts with whsec_) into LINQ_WEBHOOK_SECRET "
                     "in Render, then save and deploy. This page switches off once it's set.",
        "linq_reply": reply,
    })


@app.post("/webhooks/vapi")
def vapi_webhook():
    """Vapi posts here when a call ends. Answer right away; save the story
    in the background so Vapi isn't kept waiting."""
    if not calls.webhook_authorized(request.headers):
        log.warning("Rejected a Vapi webhook with a missing or wrong secret header")
        return jsonify({"error": "unauthorized"}), 401
    message = (request.get_json(silent=True) or {}).get("message") or {}
    if message.get("type") == "end-of-call-report":
        threading.Thread(target=_safe_end_of_call, args=(message,), daemon=True).start()
    return jsonify({"ok": True})


def _safe_end_of_call(message: dict) -> None:
    try:
        calls.handle_end_of_call(message)
    except Exception:
        log.exception("Saving the story after the call failed")


@app.get("/health")
def health():
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}


if __name__ == "__main__":
    app.run(debug=True, port=5000)

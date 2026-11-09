"""Storyline: Flask routes only. The real work lives in services/."""

import hmac
import json
import logging
import tempfile
import threading
import time
from pathlib import Path

from flask import Flask, abort, jsonify, redirect, render_template, request, url_for

from services import calls, claude_ai, config, knowledge, sheets

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

SAMPLE_CALL = config.ROOT / "tests" / "fixtures" / "end_of_call_report.json"


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


MESSAGES = {
    "calling": ("ok", "Calling you now. Pick up and say hello to Storyline!"),
    "bad_code": ("error", "That demo code isn't right. It's in our project submission."),
    "bad_number": ("error", "Please enter a 10-digit US phone number."),
    "limit_number": ("error", "That number has had 3 calls today. Please try again tomorrow."),
    "limit_day": ("error", "Storyline has made all its calls for today. Please try again tomorrow."),
    "not_ready": ("error", "Calls aren't switched on yet. Please try again later."),
    "call_failed": ("error", "Sorry, the call couldn't be started. Please try again in a minute."),
}


def _code_ok(code: str) -> bool:
    expected = config.get("DEMO_CODE")
    return bool(expected) and hmac.compare_digest(code.strip().encode(), expected.encode())


@app.get("/")
def index():
    status = MESSAGES.get(request.args.get("msg", ""))
    return render_template(
        "index.html",
        grandpa_name=config.GRANDPA_NAME,
        linq_number=config.get("LINQ_PHONE_NUMBER"),
        status=status,
    )


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
        message["call"]["id"] = f"test-{int(time.time())}"
        story = calls.handle_end_of_call(message, source="test")
        if not story:
            return jsonify({"ok": False, "error": "Nothing was saved. Check Render's logs for the reason."}), 500
        return jsonify({"ok": True, "saved_to_sheet": True, "story": story})
    story = claude_ai.summarize_call(calls.call_transcript(message))
    if not story:
        return jsonify({"ok": False, "error": "Claude didn't answer. Check ANTHROPIC_API_KEY and Render's logs."}), 500
    return jsonify({"ok": True, "saved_to_sheet": False, "story": story})


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

"""Storyline: Flask routes only. The real work lives in services/."""

import logging
import tempfile
import threading
import time
from pathlib import Path

from flask import Flask, render_template

from services import config, knowledge, sheets

try:
    import fcntl  # Linux/macOS only; Render runs Linux
except ImportError:  # pragma: no cover (Windows)
    fcntl = None

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s %(levelname)s %(name)s: %(message)s",
)
log = logging.getLogger("storyline")

app = Flask(__name__)


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


@app.get("/")
def index():
    return render_template(
        "index.html",
        grandpa_name=config.GRANDPA_NAME,
        linq_number=config.get("LINQ_PHONE_NUMBER"),
    )


@app.get("/health")
def health():
    return "ok", 200, {"Content-Type": "text/plain; charset=utf-8"}


if __name__ == "__main__":
    app.run(debug=True, port=5000)

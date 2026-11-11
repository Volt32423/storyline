"""Send a Sendblue-style incoming text to a running Storyline app, as Sendblue
would, so you can test "photo texted -> photo described" without texting.

The payload (tests/fixtures/sendblue_receive.json) is synthetic, in the shape
docs.sendblue.com documents for the receive webhook, with fake phone numbers.

Usage (from the repo root, with SHEETS_WEBAPP_SECRET or SENDBLUE_WEBHOOK_SECRET
in .env or the environment, matching the running app):
    python scripts/replay_sendblue.py URL --photo IMAGE_URL  # a texted photo; any public image works
    python scripts/replay_sendblue.py URL --text "hi"        # a text with no photo

The message handle changes on every run, so each replay is processed. The
reply to the fake sender fails, which is expected.
"""

import json
import sys
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import config, sendblue  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "sendblue_receive.json"


def main() -> int:
    args = sys.argv[1:]
    base = (args[0] if args and not args[0].startswith("--") else config.get("PUBLIC_BASE_URL")).rstrip("/")
    secret = sendblue.webhook_secret()
    if not (base and secret):
        print("Give the app's URL (or set PUBLIC_BASE_URL), and set SHEETS_WEBAPP_SECRET or SENDBLUE_WEBHOOK_SECRET.")
        return 1
    message = json.loads(FIXTURE.read_text(encoding="utf-8"))
    message["message_handle"] = f"replay-{uuid.uuid4().hex[:12]}"
    if "--photo" in args:
        message["media_url"] = args[args.index("--photo") + 1]
    if "--text" in args:
        message["content"] = args[args.index("--text") + 1]
        if "--photo" not in args:
            message["media_url"] = ""
    response = requests.post(
        f"{base}/webhooks/sendblue",
        json=message,
        headers={sendblue.SECRET_HEADER: secret},
        timeout=30,
    )
    print(f"{response.status_code} {response.text.strip()}")
    return 0 if response.ok else 1


if __name__ == "__main__":
    sys.exit(main())

"""Send a signed Linq message.received event to a running Storyline app, as
Linq would, so you can test "photo texted -> photo described" without texting.

Usage (from the repo root, with LINQ_WEBHOOK_SECRET in .env or the environment):
    python scripts/replay_linq.py URL                    # sample event, sample photo URL
    python scripts/replay_linq.py URL --photo IMAGE_URL  # point the photo at any public image

The event id changes on every run, so each replay is processed.
"""

import base64
import hashlib
import hmac
import json
import sys
import time
import uuid
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import config  # noqa: E402

FIXTURE = ROOT / "tests" / "fixtures" / "message_received.json"


def sign(secret: str, msg_id: str, timestamp: str, body: bytes) -> str:
    key = base64.b64decode(secret.removeprefix("whsec_"))
    digest = hmac.new(key, f"{msg_id}.{timestamp}.".encode() + body, hashlib.sha256).digest()
    return "v1," + base64.b64encode(digest).decode()


def main() -> int:
    args = sys.argv[1:]
    base = (args[0] if args and not args[0].startswith("--") else config.get("PUBLIC_BASE_URL")).rstrip("/")
    secret = config.get("LINQ_WEBHOOK_SECRET")
    if not (base and secret):
        print("Give the app's URL (or set PUBLIC_BASE_URL), and set LINQ_WEBHOOK_SECRET.")
        return 1
    event = json.loads(FIXTURE.read_text(encoding="utf-8"))
    event["event_id"] = f"replay-{uuid.uuid4().hex[:12]}"
    if "--photo" in args:
        event["data"]["parts"][0]["url"] = args[args.index("--photo") + 1]
    body = json.dumps(event).encode()
    msg_id, timestamp = f"msg_{uuid.uuid4().hex[:16]}", str(int(time.time()))
    response = requests.post(
        f"{base}/webhooks/linq",
        data=body,
        headers={
            "Content-Type": "application/json",
            "webhook-id": msg_id,
            "webhook-timestamp": timestamp,
            "webhook-signature": sign(secret, msg_id, timestamp, body),
        },
        timeout=30,
    )
    print(f"{response.status_code} {response.text.strip()}")
    return 0 if response.ok else 1


if __name__ == "__main__":
    sys.exit(main())

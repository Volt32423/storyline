"""Send a saved end-of-call report to a running Storyline app, as Vapi would,
so you can test "hang up -> story saved" without paying for a phone call.

Usage (from the repo root, with SHEETS_WEBAPP_SECRET in .env or the environment):
    python scripts/replay_vapi.py                         # to PUBLIC_BASE_URL
    python scripts/replay_vapi.py http://localhost:5000   # to a local app
    python scripts/replay_vapi.py URL path/to/report.json # a different payload

The replayed call id starts with "replay-", so its story row is easy to spot
(and delete) in the Sheet. Replaying the same file twice saves it only once.
"""

import json
import sys
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import calls, config  # noqa: E402

DEFAULT_FIXTURE = ROOT / "tests" / "fixtures" / "end_of_call_report.json"


def main() -> int:
    base = (sys.argv[1] if len(sys.argv) > 1 else config.get("PUBLIC_BASE_URL")).rstrip("/")
    if not base:
        print("Give the app's URL, or set PUBLIC_BASE_URL.")
        return 1
    fixture = Path(sys.argv[2]) if len(sys.argv) > 2 else DEFAULT_FIXTURE
    payload = json.loads(fixture.read_text(encoding="utf-8"))
    response = requests.post(
        f"{base}/webhooks/vapi",
        json=payload,
        headers={calls.WEBHOOK_HEADER: calls.webhook_secret()},
        timeout=30,
    )
    print(f"{response.status_code} {response.text.strip()}")
    if response.ok:
        print("Sent. The story should appear in the Sheet's stories tab within about 30 seconds.")
    return 0 if response.ok else 1


if __name__ == "__main__":
    sys.exit(main())

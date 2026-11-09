"""Set up the Google Sheet by hand: create the tabs and load Frank's seed stories.

You normally don't need this: the app does the same thing by itself every
time it starts on Render. It's here for local testing.

Usage (from the repo root):
    python scripts/seed_sheet.py            # create tabs + load seed stories
    python scripts/seed_sheet.py --dry-run  # just print what would be loaded

Safe to run more than once: tabs that exist are kept, and a seed story whose
title is already in the Sheet is skipped.
"""

import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from services import knowledge  # noqa: E402


def main() -> int:
    stories = knowledge.parse_seed_stories()

    if "--dry-run" in sys.argv:
        print(f"Found {len(stories)} seed stories in knowledge/profile.md:\n")
        for story in stories:
            print(f"- {story['title']}")
            print(f"  People:  {story['people']}")
            print(f"  Places:  {story['places']}")
            print(f"  Summary: {story['summary']}\n")
        return 0

    from services import sheets

    added = knowledge.seed_sheet()
    print(f"Done. Tabs are ready and {added} seed stories were added.")
    print(f"Sheet: {sheets.sheet_url()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

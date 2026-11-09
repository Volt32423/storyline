"""Set up the Google Sheet: create the tabs and load Frank's seed stories.

Usage (from the repo root):
    python scripts/seed_sheet.py            # create tabs + load seed stories
    python scripts/seed_sheet.py --dry-run  # just print what would be loaded

Safe to run more than once: tabs that exist are kept, and a seed story whose
title is already in the Sheet is skipped.
"""

import re
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

PROFILE = ROOT / "knowledge" / "profile.md"
SECTION = "## Stories Frank has already told"
FIELD = re.compile(r"^-\s+\*\*(People|Places|Summary):\*\*\s*(.*)$")


def parse_seed_stories(text: str) -> list[dict]:
    """Return [{title, people, places, summary}, ...] from the profile's
    'Stories Frank has already told' section."""
    start = text.find(SECTION)
    if start == -1:
        raise ValueError(f"Couldn't find '{SECTION}' in {PROFILE}")
    body = text[start + len(SECTION):]
    next_section = re.search(r"^## ", body, flags=re.MULTILINE)
    if next_section:
        body = body[: next_section.start()]

    stories = []
    for block in re.split(r"^### ", body, flags=re.MULTILINE)[1:]:
        lines = block.strip().splitlines()
        story = {"title": lines[0].strip(), "people": "", "places": "", "summary": ""}
        for line in lines[1:]:
            match = FIELD.match(line.strip())
            if match:
                story[match.group(1).lower()] = match.group(2).strip()
        if not story["summary"]:
            raise ValueError(f"Seed story '{story['title']}' has no Summary line")
        stories.append(story)
    return stories


def main() -> int:
    stories = parse_seed_stories(PROFILE.read_text(encoding="utf-8"))

    if "--dry-run" in sys.argv:
        print(f"Found {len(stories)} seed stories in {PROFILE.relative_to(ROOT)}:\n")
        for story in stories:
            print(f"- {story['title']}")
            print(f"  People:  {story['people']}")
            print(f"  Places:  {story['places']}")
            print(f"  Summary: {story['summary']}\n")
        return 0

    from services import sheets  # needs the Google env vars

    sheets.ensure_tabs()
    existing = {row["title"] for row in sheets.rows("stories") if row.get("source") == "seed"}
    added = 0
    for story in stories:
        if story["title"] in existing:
            print(f"Already there: {story['title']}")
            continue
        sheets.append("stories", {
            "timestamp": sheets.now_iso(),
            "title": story["title"],
            "summary": story["summary"],
            "people": story["people"],
            "places": story["places"],
            "concern": "",
            "source": "seed",
            "call_id": "",
        })
        added += 1
        print(f"Added: {story['title']}")

    print(f"\nDone. Tabs are ready and {added} seed stories were added.")
    print(f"Sheet: {sheets.sheet_url()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())

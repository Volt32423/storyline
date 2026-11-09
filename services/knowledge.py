"""Frank's profile (knowledge/profile.md) and the seed stories inside it."""

import logging
import re

from services import config, sheets

log = logging.getLogger(__name__)

PROFILE = config.ROOT / "knowledge" / "profile.md"
SECTION = "## Stories Frank has already told"
FIELD = re.compile(r"^-\s+\*\*(People|Places|Summary):\*\*\s*(.*)$")


def profile_text() -> str:
    return PROFILE.read_text(encoding="utf-8")


def profile_for_prompt() -> str:
    """The profile without its seed-story section: saved stories come from the
    Sheet instead, so they'd otherwise appear twice in the call prompt."""
    # Drop the "> Fictional..." note too: it's for people reading the file, and
    # the AI shouldn't tell callers that Frank is made up.
    text = "\n".join(line for line in profile_text().splitlines() if not line.startswith(">"))
    start = text.find(SECTION)
    if start == -1:
        return text.strip()
    rest = text[start + len(SECTION):]
    next_section = re.search(r"^## ", rest, flags=re.MULTILINE)
    tail = rest[next_section.start():] if next_section else ""
    return (text[:start] + tail).strip()


def new_topics() -> list[str]:
    """Bullets under 'Topics he hasn't talked about yet'."""
    match = re.search(r"^## Topics he hasn't talked about yet\s*$(.*?)(?=^## |\Z)",
                      profile_text(), flags=re.MULTILINE | re.DOTALL)
    if not match:
        return []
    return [line.strip()[2:].strip() for line in match.group(1).splitlines()
            if line.strip().startswith("- ")]


def parse_seed_stories(text: str | None = None) -> list[dict]:
    """Return [{title, people, places, summary}, ...] from the profile's
    'Stories Frank has already told' section."""
    text = profile_text() if text is None else text
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


def seed_sheet() -> int:
    """Create any missing tabs and add any seed story whose title isn't in the
    Sheet yet. Safe to run any number of times. Returns how many were added."""
    sheets.ensure_tabs()
    existing = {row["title"] for row in sheets.rows("stories") if row.get("source") == "seed"}
    added = 0
    for story in parse_seed_stories():
        if story["title"] in existing:
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
        log.info("Seed story added: %s", story["title"])
    return added

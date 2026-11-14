"""Texting (the Texting superpower): which texting service is on, and the
text the family gets after each call.

Two services can do it, and the code for both stays in the repo:
- Sendblue (services/sendblue.py) is used when SENDBLUE_API_KEY,
  SENDBLUE_API_SECRET and SENDBLUE_PHONE_NUMBER are set. On its free plan, only
  phones added as verified contacts can text Storyline.
- Linq (services/linq.py) is used when LINQ_API_TOKEN is set and Sendblue isn't.
"""

from services import config, linq, sendblue


def provider() -> str:
    """"sendblue", "linq", or "" when texting isn't set up."""
    if sendblue.is_configured():
        return "sendblue"
    if linq.is_configured():
        return "linq"
    return ""


def phone_number() -> str:
    """The number people text photos to (E.164), or "" when texting is off."""
    return {"sendblue": config.get("SENDBLUE_PHONE_NUMBER"),
            "linq": config.get("LINQ_PHONE_NUMBER")}.get(provider(), "")


def open_to_everyone() -> bool:
    """Can anyone text Storyline's number? Not on Sendblue's free plan, which
    only passes on texts from the family's verified phones, so the page
    doesn't ask judges to text it."""
    return provider() == "linq"


def sending_enabled() -> bool:
    return config.flag("SMS_SENDING_ENABLED", True) and bool(provider())


def send_to_number(number: str, text: str) -> bool:
    """Text a phone number (E.164) with whichever service is on. Never raises."""
    service = provider()
    if service == "sendblue":
        return sendblue.send(number, text)
    if service == "linq":
        return linq.send_to_number(number, text)
    return False


def send_family_concern(concern: str, source: str = "call") -> None:
    """After a call with no story: still tell the family what he said about
    his health or mood, since Storyline told him it would."""
    number = config.get("FAMILY_PHONE_NUMBER")
    if not (number and concern and sending_enabled()):
        return
    prefix = "(Test) " if source == "test" else ""
    send_to_number(number, f"{prefix}From Grandpa {config.GRANDPA_NAME}'s call today, worth checking on: {concern}")


def send_family_story(story: dict) -> None:
    """After a call: text the new story (or the fuller version of one he
    added to) to FAMILY_PHONE_NUMBER."""
    number = config.get("FAMILY_PHONE_NUMBER")
    if not (number and sending_enabled()):
        return
    name = config.GRANDPA_NAME
    prefix = "(Test) " if story.get("source") == "test" else ""
    if story.get("adds_to"):
        text = f"{prefix}Grandpa {name} added to a story: {story['title']}\n\n{story['summary']}"
    else:
        text = f"{prefix}New story from Grandpa {name}: {story['title']}\n\n{story['summary']}"
    if story.get("concern"):
        text += f"\n\nWorth checking on: {story['concern']}"
    send_to_number(number, text)

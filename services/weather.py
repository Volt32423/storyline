"""Today's weather in Frank's city, from OpenWeather (the Real-world data superpower)."""

import logging
import time

import requests

from services import config

log = logging.getLogger(__name__)

TIMEOUT_SECONDS = 8
CACHE_SECONDS = 600  # weather doesn't change much in 10 minutes

_cache: dict = {"at": 0.0, "value": None}


def city_name() -> str:
    """'Pittsburgh,US' -> 'Pittsburgh'."""
    return config.GRANDPA_CITY.split(",")[0].strip()


def last() -> dict | None:
    """The last weather reading, however old, or None."""
    return _cache["value"]


def current() -> dict | None:
    """Return {'temp': 48, 'description': 'light rain', 'city': 'Pittsburgh'},
    or None if the key is missing or OpenWeather can't be reached. Never raises."""
    if _cache["value"] and time.time() - _cache["at"] < CACHE_SECONDS:
        return _cache["value"]
    key = config.get("OPENWEATHER_API_KEY")
    if not key:
        log.warning("No OPENWEATHER_API_KEY set, so calls won't mention the weather")
        return None
    base = config.get("OPENWEATHER_BASE_URL", "https://api.openweathermap.org")
    try:
        response = requests.get(
            f"{base}/data/2.5/weather",
            params={"q": config.GRANDPA_CITY, "units": "imperial", "appid": key},
            timeout=TIMEOUT_SECONDS,
        )
        response.raise_for_status()
        data = response.json()
        value = {
            "temp": round(data["main"]["temp"]),
            "description": data["weather"][0]["description"],
            "city": city_name(),
        }
    except requests.HTTPError as error:
        # Don't log the URL: it contains the API key.
        status = error.response.status_code if error.response is not None else "?"
        hint = " (new OpenWeather keys can take up to 2 hours to activate)" if status == 401 else ""
        log.error("Weather lookup failed: OpenWeather returned HTTP %s%s", status, hint)
        return None
    except Exception as error:
        log.error("Weather lookup failed: %s", type(error).__name__)
        return None
    _cache.update(at=time.time(), value=value)
    log.info("Weather in %s: %s°F, %s", value["city"], value["temp"], value["description"])
    return value

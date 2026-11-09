"""Settings from environment variables.

On Render, the variables are set in the dashboard. For local runs, a `.env`
file in the repo root is loaded once at import (it never overrides variables
that are already set). See `.env.example` for every name.
"""

import os
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent


def _load_dotenv(path: Path) -> None:
    if not path.exists():
        return
    for raw in path.read_text(encoding="utf-8").splitlines():
        line = raw.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip()
        # Allow an inline comment after whitespace: KEY=value   # note
        if " #" in value and not value.startswith(("'", '"')):
            value = value.split(" #", 1)[0].rstrip()
        if len(value) >= 2 and value[0] == value[-1] and value[0] in "'\"":
            value = value[1:-1]
        os.environ.setdefault(key, value)


_load_dotenv(ROOT / ".env")


def get(name: str, default: str = "") -> str:
    """Return a setting, or `default` when it's missing or blank."""
    value = os.environ.get(name, "").strip()
    return value or default


def require(name: str) -> str:
    """Return a setting, or raise a clear error when it's missing."""
    value = get(name)
    if not value:
        raise RuntimeError(f"Missing environment variable {name}. See .env.example.")
    return value


def flag(name: str, default: bool = False) -> bool:
    value = get(name)
    if not value:
        return default
    return value.lower() in {"1", "true", "yes", "on"}


def number(name: str, default: int) -> int:
    try:
        return int(get(name, str(default)))
    except ValueError:
        return default


GRANDPA_NAME = get("GRANDPA_NAME", "Frank")
GRANDPA_CITY = get("GRANDPA_CITY", "Pittsburgh,US")
BUILDER_NAME = get("BUILDER_NAME", "Sam")

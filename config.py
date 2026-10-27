"""Configuration loaded from environment variables or a local .env file.

INSTAGRAM_COOKIES is a cookie header from your own Instagram session.
GEMINI_API_KEY is optional; it is never stored in the report or source.
GEMINI_MODEL defaults to the Gemini Flash model and can be overridden.
"""
import math
import os
from pathlib import Path


def _load_dotenv():
    """Load simple KEY=VALUE entries without overriding process variables."""
    env_path = Path(__file__).resolve().parent / ".env"
    if not env_path.is_file():
        return
    for line in env_path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        key = key.strip()
        value = value.strip().strip('"').strip("'")
        if key and key not in os.environ:
            os.environ[key] = value


_load_dotenv()


def _seconds_setting(name, default, minimum=0.0):
    try:
        value = float(os.environ.get(name, default))
    except (TypeError, ValueError):
        value = float(default)
    return max(minimum, value) if math.isfinite(value) else float(default)

# Retry policy for transport errors and blocks (every request).
MAX_RETRIES = 3
RETRY_BACKOFF = 2
INSTAGRAM_PROXY = os.environ.get("INSTAGRAM_PROXY") or None
INSTAGRAM_FALLBACK_PROXY_COUNTRY = None
INSTAGRAM_DIRECT_COOLDOWN = 900
INSTAGRAM_REQUEST_INTERVAL_SECONDS = _seconds_setting(
    "INSTAGRAM_REQUEST_INTERVAL_SECONDS", 1.5, minimum=1.0
)
INSTAGRAM_RATE_LIMIT_COOLDOWN_SECONDS = _seconds_setting(
    "INSTAGRAM_RATE_LIMIT_COOLDOWN_SECONDS", 900, minimum=1.0
)


def instagram_proxy():
    """The proxy every request goes through (None = direct)."""
    return os.environ.get("INSTAGRAM_PROXY") or None


def instagram_cookies():
    """Cookie header from environment or the local .env file."""
    return os.environ.get("INSTAGRAM_COOKIES") or None


def gemini_api_key():
    """Gemini API key from environment or the local .env file."""
    return os.environ.get("GEMINI_API_KEY") or None


def gemini_model():
    """Gemini model name, overridable for compatibility with account access."""
    return os.environ.get("GEMINI_MODEL", "gemini-3.5-flash-lite").strip()


def instagram_fallback_proxy():
    """No fallback exit in the standalone package."""
    return None

"""Server-side storage for the Instagram session cookies."""

from __future__ import annotations

import os
import threading
from pathlib import Path

BASE_DIR = Path(__file__).resolve().parent.parent
COOKIES_FILE = Path(os.environ.get("IG_COOKIES_FILE", BASE_DIR / "cookies.txt"))

COOKIE_KEY = "instgrab:cookies"
META_KEY = "instgrab:cookies:meta"

_lock = threading.Lock()
_client = None
_client_url = None


class StorageError(Exception):
    """Raised when the cookie store cannot be reached."""


def _redis_url() -> str | None:
    url = os.environ.get("REDIS_URL") or os.environ.get("KV_URL")
    return url.strip() if url and url.strip() else None


def _get_client():
    global _client, _client_url

    url = _redis_url()
    if not url:
        return None

    if _client is not None and _client_url == url:
        return _client

    with _lock:
        if _client is None or _client_url != url:
            try:
                import redis
            except ImportError as exc:
                raise StorageError("redis package is not installed") from exc
            _client = redis.from_url(
                url,
                socket_timeout=3,
                socket_connect_timeout=3,
                decode_responses=True,
            )
            _client_url = url
    return _client


def storage_backend() -> str:
    return "redis" if _redis_url() else "file"


def get_cookie_text() -> str:
    """Return the stored cookie text, preferring the environment."""

    env_value = (os.environ.get("IG_COOKIES") or "").strip()
    if env_value:
        return env_value

    client = _get_client()
    if client is not None:
        try:
            value = client.get(COOKIE_KEY)
        except Exception as exc:
            raise StorageError("Could not read the cookie store.") from exc
        if value:
            return value
        return ""

    try:
        return COOKIES_FILE.read_text(encoding="utf-8").strip()
    except OSError:
        return ""


def save_cookie_text(text: str) -> None:
    text = (text or "").strip()
    if not text:
        raise StorageError("No cookie data was provided.")

    client = _get_client()
    if client is not None:
        try:
            pipeline = client.pipeline()
            pipeline.set(COOKIE_KEY, text)
            pipeline.set(META_KEY, "saved")
            pipeline.execute()
        except Exception as exc:
            raise StorageError("Could not save the cookie store.") from exc
        return

    try:
        COOKIES_FILE.write_text(text + "\n", encoding="utf-8")
        try:
            os.chmod(COOKIES_FILE, 0o600)
        except OSError:
            pass
    except OSError as exc:
        raise StorageError("Could not write cookies.txt.") from exc


def clear_cookie_text() -> None:
    client = _get_client()
    if client is not None:
        try:
            client.delete(COOKIE_KEY, META_KEY)
        except Exception as exc:
            raise StorageError("Could not clear the cookie store.") from exc
        return

    try:
        COOKIES_FILE.unlink(missing_ok=True)
    except OSError as exc:
        raise StorageError("Could not remove cookies.txt.") from exc


def has_stored_cookies() -> bool:
    try:
        return bool(get_cookie_text())
    except StorageError:
        return False

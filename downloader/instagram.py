"""Instagram Story / Post / Reel downloader services."""

from __future__ import annotations

import http.cookiejar
import http.cookies
import json
import os
import re
import shutil
import tempfile
import time
import uuid
import zipfile
from pathlib import Path
from urllib.parse import quote, urlparse

import requests

from .session_store import StorageError, get_cookie_text

BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = BASE_DIR / "downloads"

JOB_TTL_SECONDS = 3 * 60 * 60
REQUEST_TIMEOUT = (15, 90)
ZIP_NAME = "_instagram_media.zip"

INSTAGRAM = "https://www.instagram.com"
USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/147.0.0.0 Safari/537.36"
)
API_HEADERS = {
    "X-IG-App-ID": "936619743392459",
    "X-ASBD-ID": "359341",
    "X-IG-WWW-Claim": "0",
    "Origin": INSTAGRAM,
    "Accept": "*/*",
}
WEB_HEADERS = {
    "Accept": "text/html,application/xhtml+xml,application/xml;q=0.9,*/*;q=0.8",
    "Sec-Fetch-Mode": "navigate",
}
CDN_HEADERS = {
    "Accept": "*/*",
    "Referer": INSTAGRAM + "/",
}

SJS_RE = re.compile(r"<script\b[^>]+\bdata-sjs>(\{.+?\})</script>", re.S)
SHORTCODE_ALPHABET = "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-_"
SHORTCODE_RE = re.compile(r"^[A-Za-z0-9_-]+$")

IMAGE_EXTS = {"jpg", "jpeg", "png", "webp", "gif", "bmp", "avif", "heic", "heif"}
VIDEO_EXTS = {"mp4", "m4v", "mov", "webm", "mkv", "avi"}
IMAGE_CONTENT_TYPES = {
    "image/jpeg": "jpg",
    "image/jpg": "jpg",
    "image/png": "png",
    "image/webp": "webp",
    "image/gif": "gif",
    "image/avif": "avif",
    "image/heic": "heic",
    "image/heif": "heif",
}
VIDEO_CONTENT_TYPES = {
    "video/mp4": "mp4",
    "video/quicktime": "mov",
    "video/webm": "webm",
    "video/x-matroska": "mkv",
}

URL_PATTERNS = (
    re.compile(r"^/stories/[A-Za-z0-9._]+(?:/\d+)?/?$"),
    re.compile(r"^/(?:p|reel|reels|tv)/[A-Za-z0-9_-]+/?$"),
)

LOGIN_HINT = (
    "Instagram is asking for a logged-in session. Stories (and some posts) "
    "require it. Add your Instagram session cookie to the IG_SESSION_ID "
    "environment variable, or export your browser cookies in Netscape format "
    "and set them as IG_COOKIES (locally you can also save them as cookies.txt "
    "in the project folder). Then redeploy the service."
)


class DownloadError(Exception):
    """Raised when Instagram content cannot be fetched."""


# ---------------------------------------------------------------------------
# URL parsing
# ---------------------------------------------------------------------------


def parse_instagram_url(raw: object) -> tuple[str, dict]:
    if not isinstance(raw, str) or not raw.strip():
        raise ValueError("Paste an Instagram link first.")

    raw = raw.strip()
    if not re.match(r"^https?://", raw, re.IGNORECASE):
        raw = "https://" + raw

    parsed = urlparse(raw)
    host = (parsed.hostname or "").lower()
    if host not in {"instagram.com", "www.instagram.com", "m.instagram.com"}:
        raise ValueError("That doesn't look like an Instagram link.")

    path = re.sub(r"/{2,}", "/", parsed.path or "/")
    if not any(pattern.match(path) for pattern in URL_PATTERNS):
        raise ValueError(
            "Unsupported link. Paste an Instagram story, post or reel URL."
        )

    highlight = re.fullmatch(r"/stories/highlights/(\d+)/?", path)
    if highlight:
        return (
            f"{INSTAGRAM}{path}",
            {"type": "story", "username": "highlights", "story_id": highlight.group(1)},
        )

    story = re.fullmatch(r"/stories/([A-Za-z0-9._]+)(?:/(\d+))?/?", path)
    if story:
        return (
            f"{INSTAGRAM}{path}",
            {
                "type": "story",
                "username": story.group(1),
                "story_id": story.group(2),
            },
        )

    post = re.fullmatch(r"/(p|reel|reels|tv)/([A-Za-z0-9_-]+)/?", path)
    if post:
        kind = post.group(1)
        if kind == "reels":
            kind = "reel"
        return (
            f"{INSTAGRAM}{path}",
            {"type": "post", "kind": kind, "shortcode": post.group(2)},
        )

    raise ValueError("Unsupported link. Paste an Instagram story, post or reel URL.")


# ---------------------------------------------------------------------------
# Instagram client helpers
# ---------------------------------------------------------------------------


def derive_user_id(sessionid: str) -> str:
    from urllib.parse import unquote

    decoded = unquote(sessionid or "")
    first = decoded.split(":", 1)[0]
    return first if first.isdigit() else ""


def ensure_ds_user_id(session: requests.Session) -> None:
    if any(cookie.name == "ds_user_id" for cookie in session.cookies):
        return
    sessionid = next(
        (cookie.value for cookie in session.cookies if cookie.name == "sessionid"),
        "",
    )
    user_id = derive_user_id(sessionid)
    if user_id:
        session.cookies.set("ds_user_id", user_id, domain=".instagram.com")


def apply_cookie_text(session: requests.Session, text: str) -> None:
    text = (text or "").strip()
    if not text:
        return

    if "\t" in text or text.startswith("# Netscape"):
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", delete=False, suffix=".txt"
            ) as tmp:
                tmp.write(text)
                tmp_path = tmp.name
            jar = http.cookiejar.MozillaCookieJar(tmp_path)
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(jar)
            ensure_ds_user_id(session)
        except (OSError, http.cookiejar.LoadError):
            pass
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass
        return

    if "sessionid=" in text:
        cookie = http.cookies.SimpleCookie()
        try:
            cookie.load(text)
        except http.cookies.CookieError:
            cookie = None
        if cookie:
            for name, morsel in cookie.items():
                session.cookies.set(name, morsel.value, domain=".instagram.com")
            ensure_ds_user_id(session)
            return

    session.cookies.set("sessionid", text, domain=".instagram.com")
    ensure_ds_user_id(session)


def build_session(cookie_text: str | None = None) -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-us,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
        }
    )

    proxy = (os.environ.get("IG_PROXY") or "").strip()
    if proxy:
        session.proxies.update({"http": proxy, "https": proxy})

    if cookie_text is None:
        try:
            cookie_text = get_cookie_text()
        except StorageError:
            cookie_text = ""

    apply_cookie_text(session, cookie_text)

    session_id = os.environ.get("IG_SESSION_ID")
    if session_id:
        session.cookies.set("sessionid", session_id, domain=".instagram.com")

    try:
        session.get(INSTAGRAM + "/", headers=WEB_HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        pass

    return session


def csrf_token_from_session(session: requests.Session) -> str:
    for cookie in session.cookies:
        if cookie.name == "csrftoken":
            return cookie.value
    return ""


def api_headers_for(session: requests.Session, referer: str = "") -> dict:
    headers = dict(API_HEADERS)
    token = csrf_token_from_session(session)
    if token:
        headers["X-CSRFToken"] = token
    headers["X-Instagram-AJAX"] = "1"
    if referer:
        headers["Referer"] = referer
    return headers


def session_status() -> dict:
    try:
        cookie_text = get_cookie_text()
    except StorageError as exc:
        return {"connected": False, "verified": False, "error": str(exc)}

    if not cookie_text:
        return {"connected": False, "verified": False, "reason": "no_cookies"}

    session = build_session(cookie_text)

    try:
        response = session.get(
            f"{INSTAGRAM}/api/v1/accounts/current_user/?edit=true",
            headers=api_headers_for(session, INSTAGRAM + "/"),
            timeout=(10, 25),
        )
    except requests.RequestException:
        response = None

    if response is not None and response.ok:
        content_type = (response.headers.get("Content-Type") or "").lower()
        if "application/json" in content_type:
            try:
                user = (response.json() or {}).get("user") or {}
            except ValueError:
                user = {}
            if user.get("username"):
                return {
                    "connected": True,
                    "verified": True,
                    "username": str(user["username"]),
                    "user_id": str(user.get("pk") or user.get("id") or ""),
                }

    has_session = any(
        cookie.name in {"sessionid", "ds_user_id"} for cookie in session.cookies
    )
    return {"connected": has_session, "verified": False}


def shortcode_to_pk(shortcode: str) -> str:
    if not SHORTCODE_RE.match(shortcode):
        raise ValueError("Invalid Instagram shortcode.")
    value = 0
    for char in shortcode:
        value = value * 64 + SHORTCODE_ALPHABET.index(char)
    return str(value)


def walk_for_key(value: object, key: str, results: list, depth: int = 0) -> None:
    if depth > 40:
        return
    if isinstance(value, dict):
        candidate = value.get(key)
        if isinstance(candidate, dict):
            results.append(candidate)
        for child in value.values():
            walk_for_key(child, key, results, depth + 1)
    elif isinstance(value, list):
        for child in value:
            walk_for_key(child, key, results, depth + 1)


def find_polaris_media(html: str) -> dict | None:
    for script in SJS_RE.findall(html):
        try:
            data = json.loads(script)
        except ValueError:
            continue

        found: list = []
        walk_for_key(data, "xig_polaris_media", found)
        for media in found:
            if isinstance(media.get("if_not_gated_logged_out"), dict):
                return media

    return None


def find_story_user(html: str, username: str) -> dict | None:
    decoder = json.JSONDecoder()
    fallback = None
    for match in re.finditer(r'"user":\s*', html):
        try:
            value, _ = decoder.raw_decode(html[match.end():])
        except ValueError:
            continue
        if not isinstance(value, dict) or not value.get("id"):
            continue
        if str(value.get("username") or "").lower() == username.lower():
            return value
        fallback = fallback or value
    return fallback


def find_page_reels(html: str, username: str, story_id: str | None) -> dict | None:
    candidates: list[dict] = []

    for match in re.finditer(r"<script[^>]*>(.*?)</script>", html, re.S):
        body = match.group(1)
        if "xdt_api__v1__feed__reels_media__connection" not in body:
            continue
        try:
            data = json.loads(body)
        except ValueError:
            continue

        connections: list = []
        walk_for_key(data, "xdt_api__v1__feed__reels_media__connection", connections)
        for connection in connections:
            for edge in connection.get("edges") or []:
                node = (edge or {}).get("node")
                if isinstance(node, dict) and node.get("items"):
                    candidates.append(node)

    if not candidates:
        return None

    wanted_reel = f"highlight:{story_id}" if username == "highlights" else None
    for node in candidates:
        if wanted_reel and str(node.get("id") or "") == wanted_reel:
            return node

    if username != "highlights":
        for node in candidates:
            node_user = str((node.get("user") or {}).get("username") or "")
            if node_user.lower() == username.lower():
                return node

    return candidates[0]


def is_login_redirect(response: requests.Response) -> bool:
    path = urlparse(response.url).path
    return path.startswith("/accounts/login") or path == "/"


def first_line(text: str, limit: int = 120) -> str:
    text = (text or "").strip()
    if not text:
        return ""
    line = text.splitlines()[0].strip()
    return line[:limit]


def safe_name(text: str, limit: int = 60) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9._-]+", "_", (text or "").strip())
    return cleaned.strip("._-")[:limit] or "instagram"


# ---------------------------------------------------------------------------
# Extraction
# ---------------------------------------------------------------------------


def fetch_media_api(
    session: requests.Session, media_id: str, referer: str
) -> tuple[dict, list, str] | None:
    headers = api_headers_for(session, referer)
    endpoints = (
        f"{INSTAGRAM}/api/v1/media/{media_id}/info/",
        f"https://i.instagram.com/api/v1/media/{media_id}/info/",
    )
    for endpoint in endpoints:
        try:
            response = session.get(endpoint, headers=headers, timeout=REQUEST_TIMEOUT)
        except requests.RequestException:
            continue

        content_type = (response.headers.get("Content-Type") or "").lower()
        if not response.ok or "application/json" not in content_type:
            continue

        try:
            payload = response.json()
        except ValueError:
            continue

        items = payload.get("items") or []
        if not items or not isinstance(items[0], dict):
            continue

        product = items[0]
        nodes = product.get("carousel_media") or [product]
        return product, nodes, endpoint

    return None


def build_post_result(
    product: dict, nodes: list, page_url: str, shortcode: str
) -> dict:
    user = product.get("user") or {}
    username = str(user.get("username") or "instagram")
    caption = ((product.get("caption") or {}).get("text") or "").strip()
    title = first_line(caption) or f"Post by @{username}"
    thumbnail = product.get("display_uri")

    if not thumbnail and nodes:
        thumbnail = image_url(nodes[0])

    return {
        "nodes": nodes,
        "title": title,
        "uploader": username,
        "thumbnail": thumbnail,
        "referer": page_url,
        "base_name": f"{username}_{shortcode}",
    }


def extract_post(session: requests.Session, info: dict) -> dict:
    shortcode = info["shortcode"]
    kind = info["kind"]
    media_id = shortcode_to_pk(shortcode)

    ruling_url = (
        f"{INSTAGRAM}/api/v1/web/get_ruling_for_content/"
        f"?content_type=MEDIA&target_id={media_id}"
    )
    try:
        session.get(ruling_url, headers=api_headers_for(session), timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        pass

    media = None
    page_url = f"{INSTAGRAM}/{kind}/{shortcode}/"
    try:
        response = session.get(page_url, headers=WEB_HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise DownloadError(
            "Couldn't reach Instagram. Check your internet connection and try again."
        ) from exc

    if is_login_redirect(response):
        raise DownloadError(LOGIN_HINT)

    media = find_polaris_media(response.text)

    if not media and kind != "p":
        page_url = f"{INSTAGRAM}/p/{shortcode}/"
        try:
            response = session.get(page_url, headers=WEB_HEADERS, timeout=REQUEST_TIMEOUT)
            media = find_polaris_media(response.text)
        except requests.RequestException:
            media = None

    if media:
        product = media.get("if_not_gated_logged_out")
        if not isinstance(product, dict):
            raise DownloadError(
                "This post is age-restricted or only available to logged-in users. "
                + LOGIN_HINT
            )
        nodes = product.get("carousel_media") or [product]
        return build_post_result(product, nodes, page_url, shortcode)

    api_result = fetch_media_api(session, media_id, page_url)
    if api_result:
        product, nodes, _ = api_result
        return build_post_result(product, nodes, page_url, shortcode)

    raise DownloadError(
        "Couldn't load this post. It may be private, deleted, or Instagram "
        "is rate-limiting this server. Try again in a minute."
    )


def fetch_reels_api(session: requests.Session, reel_id: str) -> dict | None:
    reels_url = f"{INSTAGRAM}/api/v1/feed/reels_media/?reel_ids={quote(reel_id)}"
    try:
        response = session.get(reels_url, headers=API_HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise DownloadError(
            "Couldn't reach Instagram. Check your internet connection and try again."
        ) from exc

    content_type = (response.headers.get("Content-Type") or "").lower()
    if "application/json" not in content_type:
        return None

    try:
        payload = response.json()
    except ValueError:
        return None

    reels = payload.get("reels") or {}
    reel = reels.get(reel_id) or next(iter(reels.values()), None)
    return reel if isinstance(reel, dict) else None


def build_story_result(
    reel: dict, username: str, story_id: str | None, page_url: str
) -> dict:
    items = list(reel.get("items") or [])
    if not items:
        raise DownloadError(
            "This story has no media. It may have expired or been removed."
        )

    if story_id and username != "highlights":
        matching = [
            item
            for item in items
            if str(item.get("pk") or item.get("id")) == str(story_id)
        ]
        if matching:
            items = matching
        else:
            raise DownloadError(
                "That story item wasn't found. It may have expired already."
            )

    owner = reel.get("user") or {}
    uploader = str(owner.get("username") or username)

    first_item = items[0]
    item_user = first_item.get("user") or {}
    uploader = str(item_user.get("username") or uploader)

    caption = ""
    for item in items:
        text = ((item.get("caption") or {}).get("text") or "").strip()
        if text:
            caption = text
            break

    title = (
        first_line(reel.get("title") or "")
        or first_line(caption)
        or f"Story by @{uploader}"
    )

    thumbnail = first_item.get("display_uri") or image_url(first_item)
    identifier = story_id or str(first_item.get("pk") or "story")

    return {
        "nodes": items,
        "title": title,
        "uploader": uploader,
        "thumbnail": thumbnail,
        "referer": page_url,
        "base_name": f"{uploader}_{identifier}",
    }


def extract_story(session: requests.Session, info: dict) -> dict:
    username = info["username"]
    story_id = info.get("story_id")
    if username == "highlights" and not story_id:
        raise DownloadError("This highlight link is missing its ID.")

    page_url = f"{INSTAGRAM}/stories/{username}/"
    if story_id:
        page_url += f"{story_id}/"

    try:
        response = session.get(page_url, headers=WEB_HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException as exc:
        raise DownloadError(
            "Couldn't reach Instagram. Check your internet connection and try again."
        ) from exc

    if not is_login_redirect(response):
        reel = find_page_reels(response.text, username, story_id)
        if reel:
            return build_story_result(reel, username, story_id, page_url)
        user = find_story_user(response.text, username)
    else:
        user = None

    if username == "highlights":
        reel = fetch_reels_api(session, f"highlight:{story_id}")
        if reel:
            return build_story_result(reel, username, story_id, page_url)
        raise DownloadError(
            "Couldn't load this highlight. It may have been removed, or "
            "Instagram is rate-limiting this server. " + LOGIN_HINT
        )

    if not user or not user.get("id"):
        raise DownloadError(
            "Couldn't read this story. It may have expired, or Instagram "
            "needs a logged-in session. " + LOGIN_HINT
        )

    username = str(user.get("username") or username)
    reel = fetch_reels_api(session, str(user["id"]))
    if not reel:
        raise DownloadError(LOGIN_HINT)

    return build_story_result(reel, username, story_id, page_url)


def _response_summary(response: requests.Response | None) -> dict:
    if response is None:
        return {"status": None}
    body = response.text or ""
    lower = body.lower()
    markers = [
        marker
        for marker in (
            "accounts/login",
            "challenge",
            "checkpoint",
            "rate limit",
            "please wait",
            "sorry, this page",
            "useragent",
            "consent",
            "csrf",
            "login_required",
        )
        if marker in lower
    ]
    summary = {
        "status": response.status_code,
        "url": response.url,
        "content_type": response.headers.get("Content-Type"),
        "length": len(body),
        "markers": markers,
        "cookie_names": sorted(
            {cookie.name for cookie in response.cookies}
        ),
    }

    if "application/json" in (response.headers.get("Content-Type") or "").lower():
        try:
            payload = response.json()
        except ValueError:
            payload = None
        if isinstance(payload, dict):
            summary["json_keys"] = sorted(payload.keys())[:12]
            for key in ("message", "status", "error_type", "error_title"):
                if payload.get(key):
                    summary[key] = str(payload[key])[:200]
            summary["body_preview"] = body[:400]

    return summary


def diagnose_url(raw_url: object, use_cookies: bool = True) -> dict:
    try:
        _, info = parse_instagram_url(raw_url)
    except ValueError as exc:
        return {"ok": False, "error": str(exc)}

    session = build_session(None if use_cookies else "")
    result = {
        "ok": True,
        "type": info["type"],
        "used_cookies": use_cookies,
        "cookie_names": sorted({cookie.name for cookie in session.cookies}),
        "has_sessionid": any(
            cookie.name == "sessionid" for cookie in session.cookies
        ),
    }
    if use_cookies:
        result["session"] = session_status()

    headers = api_headers_for(session, INSTAGRAM + "/")
    headers_for_media = headers
    try:
        current_user = session.get(
            f"{INSTAGRAM}/api/v1/accounts/current_user/?edit=true",
            headers=headers,
            timeout=(10, 30),
        )
    except requests.RequestException as exc:
        result["current_user"] = {"error": type(exc).__name__}
    else:
        result["current_user"] = _response_summary(current_user)

    try:
        profile = session.get(
            f"{INSTAGRAM}/api/v1/users/web_profile_info/?username=instagram",
            headers=headers,
            timeout=(10, 30),
        )
    except requests.RequestException as exc:
        result["profile_api"] = {"error": type(exc).__name__}
    else:
        result["profile_api"] = _response_summary(profile)

    if info["type"] == "post":
        shortcode = info["shortcode"]
        media_id = shortcode_to_pk(shortcode)
        page_url = f"{INSTAGRAM}/{info['kind']}/{shortcode}/"
        try:
            page = session.get(page_url, headers=WEB_HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            result["page"] = {"error": type(exc).__name__}
        else:
            result["page"] = _response_summary(page)
            result["has_polaris"] = bool(find_polaris_media(page.text))
        attempts = []
        for endpoint in (
            f"{INSTAGRAM}/api/v1/media/{media_id}/info/",
            f"https://i.instagram.com/api/v1/media/{media_id}/info/",
        ):
            try:
                attempt = session.get(
                    endpoint, headers=headers_for_media, timeout=REQUEST_TIMEOUT
                )
            except requests.RequestException as exc:
                attempts.append({"url": endpoint, "error": type(exc).__name__})
            else:
                attempts.append(_response_summary(attempt))
        result["api_media_attempts"] = attempts
    else:
        username = info["username"]
        page_url = f"{INSTAGRAM}/stories/{username}/"
        if info.get("story_id"):
            page_url += f"{info['story_id']}/"
        try:
            page = session.get(page_url, headers=WEB_HEADERS, timeout=REQUEST_TIMEOUT)
        except requests.RequestException as exc:
            result["page"] = {"error": type(exc).__name__}
        else:
            result["page"] = _response_summary(page)
            result["has_reels"] = bool(
                find_page_reels(page.text, username, info.get("story_id"))
            )

    return result


# ---------------------------------------------------------------------------
# Downloading
# ---------------------------------------------------------------------------


def image_url(node: dict) -> str | None:
    candidates = (node.get("image_versions2") or {}).get("candidates") or []
    for candidate in candidates:
        url = (candidate or {}).get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url

    fallback = node.get("display_uri")
    if isinstance(fallback, str) and fallback.startswith(("http://", "https://")):
        return fallback
    return None


def video_url(node: dict) -> str | None:
    versions = node.get("video_versions") or []
    for version in versions:
        url = (version or {}).get("url")
        if isinstance(url, str) and url.startswith(("http://", "https://")):
            return url
    return None


def choose_extension(url: str, kind: str, content_type: str) -> str:
    content_type = (content_type or "").split(";")[0].strip().lower()
    if kind == "video":
        if content_type in VIDEO_CONTENT_TYPES:
            return VIDEO_CONTENT_TYPES[content_type]
        suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
        return suffix if suffix in VIDEO_EXTS else "mp4"

    if content_type in IMAGE_CONTENT_TYPES:
        return IMAGE_CONTENT_TYPES[content_type]
    suffix = Path(urlparse(url).path).suffix.lower().lstrip(".")
    return suffix if suffix in IMAGE_EXTS else "jpg"


def stream_download(
    session: requests.Session,
    url: str,
    stem: Path,
    kind: str,
    referer: str,
) -> Path:
    headers = dict(CDN_HEADERS)
    headers["Referer"] = referer

    try:
        with session.get(
            url, headers=headers, stream=True, timeout=REQUEST_TIMEOUT
        ) as response:
            response.raise_for_status()
            content_type = response.headers.get("Content-Type") or ""
            extension = choose_extension(url, kind, content_type)
            path = stem.with_suffix("." + extension)

            with path.open("wb") as handle:
                for chunk in response.iter_content(256 * 1024):
                    if chunk:
                        handle.write(chunk)

            if path.stat().st_size == 0:
                path.unlink(missing_ok=True)
                raise DownloadError("Instagram returned an empty file.")

            return path
    except requests.RequestException as exc:
        raise DownloadError(
            "Couldn't download the media file from Instagram. The link may have "
            "expired. Please try again."
        ) from exc


def download_node(
    session: requests.Session,
    node: dict,
    index: int,
    base_name: str,
    referer: str,
    job_dir: Path,
) -> dict | None:
    video = video_url(node)
    if video:
        url = video
        kind = "video"
    else:
        url = image_url(node)
        kind = "image"

    if not url:
        return None

    stem = job_dir / f"{index:03d}_{safe_name(base_name)}"
    path = stream_download(session, url, stem, kind, referer)

    encoded = quote(path.name)
    return {
        "name": path.name,
        "url": f"/api/media/{job_dir.name}/{encoded}",
        "download_url": f"/api/media/{job_dir.name}/{encoded}?download=1",
        "type": kind,
        "size": path.stat().st_size,
        "thumbnail": image_url(node),
    }


def download_media(info: dict) -> dict:
    cleanup_old_jobs()

    session = build_session()
    if info["type"] == "story":
        extracted = extract_story(session, info)
    else:
        extracted = extract_post(session, info)

    job_id = uuid.uuid4().hex
    job_dir = DOWNLOAD_DIR / job_id
    job_dir.mkdir(parents=True, exist_ok=True)

    items = []
    try:
        for index, node in enumerate(extracted["nodes"], start=1):
            item = download_node(
                session,
                node,
                index,
                extracted["base_name"],
                extracted["referer"],
                job_dir,
            )
            if item:
                items.append(item)
    except DownloadError:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise
    except Exception:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise

    if not items:
        shutil.rmtree(job_dir, ignore_errors=True)
        raise DownloadError("No downloadable media was found at that link.")

    thumbnail = extracted.get("thumbnail")
    if items[0]["type"] == "image":
        thumbnail = items[0]["url"]
    elif isinstance(thumbnail, str) and thumbnail.startswith(("http://", "https://")):
        try:
            thumb_path = stream_download(
                session,
                thumbnail,
                job_dir / "_thumbnail",
                "image",
                extracted["referer"],
            )
            thumbnail = f"/api/media/{job_id}/{quote(thumb_path.name)}"
        except DownloadError:
            pass

    return {
        "job_id": job_id,
        "title": extracted["title"] or "Instagram media",
        "thumbnail": thumbnail,
        "uploader": extracted.get("uploader"),
        "items": items,
        "zip_url": (
            f"/api/media/{job_id}/{ZIP_NAME}?download=1" if len(items) > 1 else None
        ),
    }


def build_zip(job_dir: Path) -> bool:
    target = job_dir / ZIP_NAME
    files = sorted(
        path
        for path in job_dir.iterdir()
        if path.is_file()
        and path.name != ZIP_NAME
        and not path.name.startswith("_thumbnail")
        and path.suffix.lower() not in {".zip", ".part", ".json", ".tmp"}
    )
    if not files:
        return False

    temp = job_dir / f".{uuid.uuid4().hex}.zip.tmp"
    try:
        with zipfile.ZipFile(temp, "w", zipfile.ZIP_DEFLATED) as archive:
            for path in files:
                archive.write(path, arcname=path.name)
        os.replace(temp, target)
        return True
    finally:
        temp.unlink(missing_ok=True)


def cleanup_old_jobs() -> None:
    if not DOWNLOAD_DIR.is_dir():
        return
    cutoff = time.time() - JOB_TTL_SECONDS
    for entry in DOWNLOAD_DIR.iterdir():
        try:
            if entry.is_dir() and entry.stat().st_mtime < cutoff:
                shutil.rmtree(entry, ignore_errors=True)
        except OSError:
            continue

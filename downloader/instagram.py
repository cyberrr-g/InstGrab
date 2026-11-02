"""Instagram Story / Post / Reel downloader services."""

from __future__ import annotations

import http.cookiejar
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

BASE_DIR = Path(__file__).resolve().parent.parent
DOWNLOAD_DIR = BASE_DIR / "downloads"
COOKIES_FILE = Path(os.environ.get("IG_COOKIES_FILE", BASE_DIR / "cookies.txt"))

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


def build_session() -> requests.Session:
    session = requests.Session()
    session.headers.update(
        {
            "User-Agent": USER_AGENT,
            "Accept-Language": "en-us,en;q=0.5",
            "Accept-Encoding": "gzip, deflate, br",
        }
    )

    cookies_env = os.environ.get("IG_COOKIES")
    if cookies_env:
        tmp_path = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w", delete=False, suffix=".txt"
            ) as tmp:
                tmp.write(cookies_env)
                tmp_path = tmp.name
            jar = http.cookiejar.MozillaCookieJar(tmp_path)
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(jar)
        except (OSError, http.cookiejar.LoadError):
            pass
        finally:
            if tmp_path:
                try:
                    os.unlink(tmp_path)
                except OSError:
                    pass

    if COOKIES_FILE.is_file():
        try:
            jar = http.cookiejar.MozillaCookieJar(str(COOKIES_FILE))
            jar.load(ignore_discard=True, ignore_expires=True)
            session.cookies.update(jar)
        except (OSError, http.cookiejar.LoadError):
            pass

    session_id = os.environ.get("IG_SESSION_ID")
    if session_id:
        for domain in (".instagram.com", "www.instagram.com", "instagram.com"):
            session.cookies.set("sessionid", session_id, domain=domain)

    try:
        session.get(INSTAGRAM + "/", headers=WEB_HEADERS, timeout=REQUEST_TIMEOUT)
    except requests.RequestException:
        pass

    return session


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


def extract_post(session: requests.Session, info: dict) -> dict:
    shortcode = info["shortcode"]
    kind = info["kind"]
    media_id = shortcode_to_pk(shortcode)

    ruling_url = (
        f"{INSTAGRAM}/api/v1/web/get_ruling_for_content/"
        f"?content_type=MEDIA&target_id={media_id}"
    )
    try:
        session.get(ruling_url, headers=API_HEADERS, timeout=REQUEST_TIMEOUT)
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

    if not media:
        raise DownloadError(
            "Couldn't load this post. It may be private, deleted, or Instagram "
            "is rate-limiting this server. Try again in a minute."
        )

    product = media.get("if_not_gated_logged_out")
    if not isinstance(product, dict):
        raise DownloadError(
            "This post is age-restricted or only available to logged-in users. "
            + LOGIN_HINT
        )

    user = product.get("user") or {}
    username = str(user.get("username") or "instagram")
    caption = ((product.get("caption") or {}).get("text") or "").strip()
    nodes = product.get("carousel_media") or [product]

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

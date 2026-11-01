from __future__ import annotations

import json
import mimetypes
import re
import traceback

from django.core.exceptions import RequestDataTooBig
from django.http import (
    FileResponse,
    HttpResponse,
    JsonResponse,
    StreamingHttpResponse,
)
from django.shortcuts import render
from django.views.decorators.csrf import csrf_exempt

from .instagram import (
    DOWNLOAD_DIR,
    ZIP_NAME,
    DownloadError,
    build_zip,
    download_media,
    parse_instagram_url,
)

MAX_BODY_BYTES = 16 * 1024


def json_error(status: int, message: str) -> JsonResponse:
    return JsonResponse({"error": message}, status=status)


def index(request):
    if request.method not in {"GET", "HEAD"}:
        return json_error(404, "Not found.")
    return render(request, "index.html")


def health(request):
    if request.method not in {"GET", "HEAD"}:
        return json_error(404, "Not found.")
    return JsonResponse({"status": "ok"})


@csrf_exempt
def api_download(request):
    if request.method != "POST":
        return json_error(404, "Not found.")

    try:
        length = int(request.META.get("CONTENT_LENGTH") or 0)
    except ValueError:
        length = 0

    if length <= 0:
        return json_error(400, "Missing request body.")
    if length > MAX_BODY_BYTES:
        return json_error(413, "Request too large.")

    try:
        raw_body = request.body
    except RequestDataTooBig:
        return json_error(413, "Request too large.")

    try:
        payload = json.loads(raw_body.decode("utf-8"))
    except (UnicodeDecodeError, ValueError):
        return json_error(400, "Invalid JSON body.")

    if not isinstance(payload, dict):
        return json_error(400, "Invalid request body.")

    try:
        _, info = parse_instagram_url(payload.get("url"))
    except ValueError as exc:
        return json_error(400, str(exc))

    try:
        result = download_media(info)
    except DownloadError as exc:
        return json_error(502, str(exc))
    except Exception:
        traceback.print_exc()
        return json_error(
            500,
            "Something went wrong while downloading. Please try again.",
        )

    return JsonResponse(result)


def parse_byte_range(header: str | None, size: int) -> tuple[int, int] | str | None:
    if not header:
        return None
    match = re.fullmatch(r"bytes=(\d*)-(\d*)", header.strip())
    if not match:
        return None

    start_text, end_text = match.groups()
    if not start_text and not end_text:
        return None

    if not start_text:
        suffix = int(end_text)
        if suffix <= 0:
            return "invalid"
        start = max(size - suffix, 0)
        end = size - 1
    else:
        start = int(start_text)
        end = int(end_text) if end_text else size - 1

    if start >= size or start > end:
        return "invalid"
    return start, min(end, size - 1)


def file_chunks(path, start: int, length: int, chunk_size: int = 256 * 1024):
    with path.open("rb") as handle:
        handle.seek(start)
        remaining = length
        while remaining > 0:
            data = handle.read(min(chunk_size, remaining))
            if not data:
                break
            remaining -= len(data)
            yield data


def media(request, job_id: str, filename: str):
    if request.method not in {"GET", "HEAD"}:
        return json_error(404, "File not found.")

    force_download = request.GET.get("download", "0").lower() in {
        "1",
        "true",
        "yes",
    }

    if not re.fullmatch(r"[a-f0-9]{32}", job_id) or not filename:
        return json_error(404, "File not found.")

    job_dir = (DOWNLOAD_DIR / job_id).resolve()
    target = (job_dir / filename).resolve()
    try:
        target.relative_to(job_dir)
    except ValueError:
        return json_error(404, "File not found.")

    if not target.is_file() and filename == ZIP_NAME:
        try:
            created = build_zip(job_dir)
        except OSError:
            created = False
        if not created:
            return json_error(404, "File not found.")

    if not target.is_file():
        return json_error(404, "File not found.")

    content_type = mimetypes.guess_type(target.name)[0] or "application/octet-stream"
    if target.suffix.lower() == ".zip":
        content_type = "application/zip"

    size = target.stat().st_size
    disposition = "attachment" if force_download else "inline"
    ascii_name = re.sub(r"[^A-Za-z0-9._-]", "_", target.name) or "download"
    content_disposition = f'{disposition}; filename="{ascii_name}"'

    byte_range = parse_byte_range(request.headers.get("Range"), size)
    if byte_range == "invalid":
        response = HttpResponse(status=416)
        response["Content-Range"] = f"bytes */{size}"
        response["Content-Length"] = "0"
        response["Accept-Ranges"] = "bytes"
        return response

    if request.method == "HEAD":
        if byte_range:
            start, end = byte_range
            response = HttpResponse(status=206, content_type=content_type)
            response["Content-Range"] = f"bytes {start}-{end}/{size}"
            response["Content-Length"] = str(end - start + 1)
        else:
            response = HttpResponse(status=200, content_type=content_type)
            response["Content-Length"] = str(size)
    elif byte_range:
        start, end = byte_range
        length = end - start + 1
        response = StreamingHttpResponse(
            file_chunks(target, start, length),
            status=206,
            content_type=content_type,
        )
        response["Content-Range"] = f"bytes {start}-{end}/{size}"
        response["Content-Length"] = str(length)
    else:
        response = FileResponse(target.open("rb"), content_type=content_type)
        response["Content-Length"] = str(size)

    response["Accept-Ranges"] = "bytes"
    response["Content-Disposition"] = content_disposition
    response["Cache-Control"] = "public, max-age=3600"
    response["X-Content-Type-Options"] = "nosniff"
    return response

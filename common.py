"""Shared validation, atomic output and bounded HTTPS helpers."""
from __future__ import annotations

import hashlib
import json
import os
import re
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zlib
from datetime import datetime, timezone
from pathlib import Path


class UserError(Exception):
    """Only explicitly sanitized messages may be placed in this exception."""


class ApiError(UserError):
    def __init__(self, service, code=None):
        self.code = code
        super().__init__(f"{service}: request failed" + (f" (HTTP {code})" if code else ""))


def utcnow():
    return datetime.now(timezone.utc).isoformat()


def read_json(path):
    try:
        return json.loads(Path(path).read_text(encoding="utf-8-sig"))
    except (OSError, ValueError):
        raise UserError("Cannot read JSON file; check path, permissions and JSON syntax") from None


def atomic_json(path, data):
    path = Path(path)
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(path.name + "." + uuid.uuid4().hex + ".tmp")
    try:
        with temporary.open("w", encoding="utf-8") as stream:
            json.dump(data, stream, ensure_ascii=False, indent=2)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(temporary, path)
    finally:
        temporary.unlink(missing_ok=True)


def identifier(value, label):
    if not isinstance(value, str) or not re.fullmatch(r"[A-Za-z0-9_-]{1,128}", value):
        raise UserError(f"Invalid {label}; use letters, digits, underscore or hyphen")
    return value


def require_keys(data, allowed, label):
    if not isinstance(data, dict) or set(data) - set(allowed):
        raise UserError(f"Invalid or unknown fields in {label}")


class NoRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        return None


class Http:
    """Tokens stay in headers; response bodies and URLs never enter errors."""
    def __init__(self, timeout=60):
        self.timeout = timeout
        self.opener = urllib.request.build_opener(NoRedirect())

    def open(self, method, url, token, params=None, body=None, headers=None):
        if urllib.parse.urlsplit(url).scheme != "https":
            raise UserError("Only HTTPS endpoints are supported")
        request_headers = {"Authorization": "Bearer " + token, **(headers or {})}
        if params is not None:
            encoded = urllib.parse.urlencode(params)
            if method == "GET":
                url += "?" + encoded
            else:
                body = encoded.encode("utf-8")
                request_headers["Content-Type"] = "application/x-www-form-urlencoded"
        if body is not None:
            request_headers["Content-Length"] = str(len(body))
        attempts = 3 if method == "GET" else 1
        for attempt in range(attempts):
            try:
                request = urllib.request.Request(url, data=body, headers=request_headers, method=method)
                return self.opener.open(request, timeout=self.timeout)
            except urllib.error.HTTPError as error:
                code = error.code
                error.close()
                if method == "GET" and (code == 429 or code >= 500) and attempt + 1 < attempts:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError("API", code) from None
            except (OSError, ValueError):
                if attempt + 1 < attempts:
                    time.sleep(2 ** attempt)
                    continue
                raise ApiError("API") from None

    def json(self, method, url, token, **kwargs):
        try:
            with self.open(method, url, token, **kwargs) as response:
                raw = response.read(2 * 1024 * 1024 + 1)
                if len(raw) > 2 * 1024 * 1024:
                    raise ValueError()
                data = json.loads(raw)
            if not isinstance(data, dict) or "error" in data:
                raise ValueError()
            return data
        except UserError:
            raise
        except (OSError, ValueError):
            raise ApiError("API response") from None


MEDIA_MIMES = {".mp4": "video/mp4", ".jpg": "image/jpeg", ".jpeg": "image/jpeg", ".png": "image/png"}


def mime_for_path(path):
    mime = MEDIA_MIMES.get(Path(path).suffix.lower())
    if mime is None:
        raise UserError("Supported file extensions: .mp4, .jpg, .jpeg, .png")
    return mime


def validate_png(stream, size):
    """Check PNG framing/CRC and reject APNG; this is not a pixel decoder."""
    stream.seek(8)
    first, image_data = True, False
    while stream.tell() < size:
        header = stream.read(8)
        if len(header) != 8:
            break
        length, kind = int.from_bytes(header[:4], "big"), header[4:]
        if length > size - stream.tell() - 4 or (first and (kind != b"IHDR" or length != 13)):
            break
        if kind in {b"acTL", b"fcTL", b"fdAT"}:
            raise UserError("Animated PNG is not supported; export a static PNG")
        crc, left = zlib.crc32(kind), length
        while left:
            block = stream.read(min(left, 1024 * 1024))
            if not block:
                raise UserError("Truncated PNG")
            if first and (int.from_bytes(block[:4], "big") == 0 or int.from_bytes(block[4:8], "big") == 0):
                raise UserError("Invalid PNG dimensions")
            crc = zlib.crc32(block, crc)
            left -= len(block)
        stored = stream.read(4)
        if len(stored) != 4 or int.from_bytes(stored, "big") != crc:
            break
        first = False
        image_data = image_data or kind == b"IDAT"
        if kind == b"IEND":
            if length == 0 and image_data and stream.tell() == size:
                return
            break
    raise UserError("Invalid or truncated PNG structure/checksum")


def inspect_media(path, max_bytes, expected_mime=None):
    path = Path(path)
    size = path.stat().st_size
    if not 0 < size <= max_bytes:
        raise UserError("Media file is empty or exceeds configured max_file_bytes")
    sha = hashlib.sha256()
    with path.open("rb") as stream:
        head = stream.read(12)
        if len(head) >= 12 and head[4:8] == b"ftyp":
            mime, kind = "video/mp4", "video"
        elif head.startswith(b"\x89PNG\r\n\x1a\n"):
            mime, kind = "image/png", "image"
            validate_png(stream, size)
        elif head.startswith(b"\xff\xd8\xff"):
            mime, kind = "image/jpeg", "image"
            stream.seek(-2, 2)
            if stream.read(2) != b"\xff\xd9":
                raise UserError("Truncated JPEG: missing end marker")
        else:
            raise UserError("Expected MP4, JPEG or static PNG content")
        if expected_mime is not None and mime != expected_mime:
            raise UserError("File content does not match extension or declared MIME type")
        stream.seek(0)
        for block in iter(lambda: stream.read(1024 * 1024), b""):
            sha.update(block)
    return {"sha256": sha.hexdigest(), "size": size, "media_type": kind, "mime_type": mime}


def inspect_video(path, max_bytes):
    media = inspect_media(path, max_bytes, expected_mime="video/mp4")
    return media["sha256"], media["size"]

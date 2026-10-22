"""Direct Meta video/image adapter with streamed multipart transfer."""
from __future__ import annotations

import hashlib
import hmac
import json
import re
import time
import uuid
from pathlib import Path

from common import Http, UserError, identifier


def remote_id(value):
    if not isinstance(value, str) or not re.fullmatch(r"[0-9]+", value):
        raise UserError("Meta response has no valid object/session ID")
    return value


class Multipart:
    def __init__(self, fields, path, start, end, field_name="video_file_chunk", filename="video.mp4", mime="application/octet-stream"):
        self.path, self.start, self.end = Path(path), start, end
        self.boundary = "ua" + uuid.uuid4().hex
        parts = []
        for key, value in fields.items():
            parts.append(f'--{self.boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode())
        parts.append((f'--{self.boundary}\r\nContent-Disposition: form-data; name="{field_name}"; '
                      f'filename="{filename}"\r\nContent-Type: {mime}\r\n\r\n').encode())
        self.prefix = b"".join(parts)
        self.suffix = f"\r\n--{self.boundary}--\r\n".encode()

    def __len__(self):
        return len(self.prefix) + self.end - self.start + len(self.suffix)

    def __iter__(self):
        yield self.prefix
        with self.path.open("rb") as stream:
            stream.seek(self.start)
            left = self.end - self.start
            while left:
                chunk = stream.read(min(left, 1024 * 1024))
                if not chunk:
                    raise UserError("Staged file was truncated during upload")
                left -= len(chunk)
                yield chunk
        yield self.suffix


class MetaClient:
    def __init__(self, version, token, account, http=None, app_secret=None):
        self.graph = f"https://graph.facebook.com/{version}"
        self.video = f"https://graph-video.facebook.com/{version}/{account}/advideos"
        self.images = f"{self.graph}/{account}/adimages"
        self.token, self.account = token, account
        self.http = http or Http(timeout=120)
        self.proof = hmac.new(app_secret.encode(), token.encode(), hashlib.sha256).hexdigest() if app_secret else None

    def request(self, method, url, params=None, **kwargs):
        params = dict(params or {})
        if self.proof:
            params["appsecret_proof"] = self.proof
        return self.http.json(method, url, self.token, params=params, **kwargs)

    def preflight(self, media_types=("video",)):
        data = self.request("GET", f"{self.graph}/{self.account}",
                              params={"fields": "id,account_status"})
        if data.get("id") != self.account or data.get("account_status") != 1:
            raise UserError("Meta account could not be verified as active")
        # Reading this edge verifies media-library read access, not guaranteed write access.
        for kind in sorted(set(media_types)):
            endpoint, field = (self.images, "hash") if kind == "image" else (f"{self.graph}/{self.account}/advideos", "id")
            listing = self.request("GET", endpoint, params={"fields": field, "limit": 1})
            if not isinstance(listing.get("data"), list):
                raise UserError("Meta media-library response is incomplete")

    def upload_image(self, path, title, mime):
        if mime not in {"image/jpeg", "image/png"}:
            raise UserError("Image upload supports JPEG and PNG only")
        extension = ".png" if mime == "image/png" else ".jpg"
        filename = (re.sub(r"[^A-Za-z0-9_-]", "_", title)[:80] or "image") + extension
        fields = {"appsecret_proof": self.proof} if self.proof else {}
        body = Multipart(fields, path, 0, Path(path).stat().st_size,
                         field_name="filename", filename=filename, mime=mime)
        reply = self.http.json("POST", self.images, self.token, body=body,
                               headers={"Content-Type": "multipart/form-data; boundary=" + body.boundary})
        images = reply.get("images")
        if not isinstance(images, dict) or len(images) != 1:
            raise UserError("Meta did not return exactly one image result")
        item = next(iter(images.values()))
        if not isinstance(item, dict):
            raise UserError("Meta returned an invalid image result")
        return identifier(item.get("hash"), "Meta image hash")

    def image_status(self, image_hash):
        image_hash = identifier(image_hash, "Meta image hash")
        params = {"fields": "hash,status", "hashes": json.dumps([image_hash]), "limit": 100}
        seen = set()
        for _ in range(1000):
            reply = self.request("GET", self.images, params=params)
            rows = reply.get("data")
            if not isinstance(rows, list) or any(not isinstance(row, dict) or "hash" not in row for row in rows):
                raise UserError("Meta image listing is incomplete")
            matches = [row for row in rows if row["hash"] == image_hash]
            if len(matches) > 1:
                raise UserError("Meta returned ambiguous image results")
            if matches:
                if matches[0].get("status") != "ACTIVE":
                    raise UserError("Image exists but Meta does not report ACTIVE status")
                return "READY"
            paging = reply.get("paging", {})
            if not paging.get("next"):
                raise UserError("Image hash was not found in the selected account")
            after = paging.get("cursors", {}).get("after")
            if not after or after in seen:
                raise UserError("Meta image listing is incomplete")
            seen.add(after)
            params["after"] = after
        raise UserError("Meta image listing exceeds limit")

    def start(self, size):
        return self.request("POST", self.video,
                              params={"upload_phase": "start", "file_size": size})

    def transfer(self, session, path, start, end):
        fields = {"upload_phase": "transfer", "upload_session_id": remote_id(session), "start_offset": start}
        if self.proof:
            fields["appsecret_proof"] = self.proof
        body = Multipart(fields, path, start, end)
        return self.http.json("POST", self.video, self.token, body=body,
                              headers={"Content-Type": "multipart/form-data; boundary=" + body.boundary})

    def finish(self, session, title):
        data = self.request("POST", self.video,
                              params={"upload_phase": "finish", "upload_session_id": remote_id(session), "title": title})
        if data.get("success") is not True:
            raise UserError("Meta did not confirm upload completion")

    def status(self, video_id):
        data = self.request("GET", f"{self.graph}/{remote_id(video_id)}", params={"fields": "status"})
        status = data.get("status")
        value = status.get("video_status") if isinstance(status, dict) else None
        if value == "ready":
            return "READY"
        if value == "error":
            return "PROCESSING_FAILED"
        if value == "processing":
            return "PROCESSING"
        raise UserError("Meta returned an unrecognized video processing status")

    def wait(self, video_id, seconds):
        deadline = time.monotonic() + seconds
        while True:
            state = self.status(video_id)
            if state != "PROCESSING" or time.monotonic() >= deadline:
                return state
            time.sleep(min(5, max(0, deadline - time.monotonic())))

    def owns(self, video_id):
        remote_id(video_id)
        params = {"fields": "id", "limit": 100}
        seen = set()
        for _ in range(1000):
            data = self.request("GET", f"{self.graph}/{self.account}/advideos", params=params)
            rows = data.get("data")
            if not isinstance(rows, list) or any(not isinstance(item, dict) or "id" not in item for item in rows):
                raise UserError("Account media listing is incomplete")
            if any(item.get("id") == video_id for item in rows):
                return True
            paging = data.get("paging", {})
            if not paging.get("next"):
                return False
            after = paging.get("cursors", {}).get("after")
            if not after or after in seen:
                raise UserError("Account media listing is incomplete; ownership could not be verified")
            seen.add(after)
            params["after"] = after
        raise UserError("Account media listing exceeds limit; ownership could not be verified")

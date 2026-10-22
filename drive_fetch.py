"""Optional read-only Drive source. Downloads are checked against metadata."""
from __future__ import annotations

import hashlib
import os
from pathlib import Path

from common import MEDIA_MIMES, Http, UserError, inspect_media


class DriveSource:
    def __init__(self, credential_env, http=None):
        self.credential_env = credential_env
        self.http = http or Http()
        self.credentials = None

    def token(self):
        try:
            from google.oauth2 import service_account
            from google.auth.transport.requests import Request
        except ImportError:
            raise UserError("Drive requires: python -m pip install -r requirements-drive.txt") from None
        path = os.environ.get(self.credential_env)
        if not path:
            raise UserError(f"Set {self.credential_env} to your service-account JSON path")
        try:
            if self.credentials is None:
                self.credentials = service_account.Credentials.from_service_account_file(
                    path, scopes=["https://www.googleapis.com/auth/drive.readonly"])
            if not self.credentials.valid:
                self.credentials.refresh(Request())
            return self.credentials.token
        except Exception:
            raise UserError("Drive authentication failed; check credentials and service-account setup") from None

    def metadata(self, file_id):
        return self.http.json("GET", "https://www.googleapis.com/drive/v3/files/" + file_id, self.token(),
                              params={"fields": "id,mimeType,size,md5Checksum,version,capabilities(canDownload)",
                                      "supportsAllDrives": "true"})

    def check(self, file_id, max_bytes):
        item = self.metadata(file_id)
        try:
            valid = (item["id"] == file_id and item["mimeType"] in MEDIA_MIMES.values()
                     and item["capabilities"]["canDownload"] is True
                     and 0 < int(item["size"]) <= max_bytes
                     and bool(item["md5Checksum"]) and bool(item["version"]))
        except (KeyError, ValueError, TypeError):
            valid = False
        if not valid:
            raise UserError("Drive source must be a downloadable MP4/JPEG/PNG with size, checksum and version metadata")
        return item

    def fetch(self, file_id, destination, max_bytes):
        before = self.check(file_id, max_bytes)
        digest = hashlib.md5(usedforsecurity=False)
        size = 0
        with self.http.open("GET", "https://www.googleapis.com/drive/v3/files/" + file_id,
                            self.token(), params={"alt": "media", "supportsAllDrives": "true"}) as response:
            with Path(destination).open("xb") as output:
                while block := response.read(1024 * 1024):
                    size += len(block)
                    if size > max_bytes:
                        raise UserError("Drive file exceeds max_file_bytes")
                    output.write(block)
                    digest.update(block)
        after = self.metadata(file_id)
        if (size != int(before["size"]) or digest.hexdigest() != before["md5Checksum"]
                or any(before.get(key) != after.get(key) for key in ("id", "version", "md5Checksum", "size", "mimeType"))):
            raise UserError("Drive file changed or download checksum does not match; rerun after source is stable")
        inspect_media(destination, max_bytes, expected_mime=before["mimeType"])
        return {"file_id": file_id, "version": before["version"], "mime_type": before["mimeType"]}

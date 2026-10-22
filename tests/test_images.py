"""Offline static-image regression and protocol tests; no credentials required."""
import contextlib
import hashlib
import io
import json
import os
import struct
import tempfile
import unittest
import zlib
from pathlib import Path
from unittest.mock import patch

from common import ApiError, UserError, inspect_media
from creative_archive import Archive
from drive_fetch import DriveSource
from meta_api import MetaClient
from meta_secrets import Profile, Settings, load_directory_jobs, load_jobs
from meta_upload import batch, main, preflight


def png_chunk(kind, data):
    return struct.pack(">I", len(data)) + kind + data + struct.pack(">I", zlib.crc32(kind + data))


def png(red=255):
    # A real, tiny one-pixel RGB PNG generated without imaging dependencies.
    return (b"\x89PNG\r\n\x1a\n" + png_chunk(b"IHDR", struct.pack(">IIBBBBB", 1, 1, 8, 2, 0, 0, 0))
            + png_chunk(b"IDAT", zlib.compress(bytes([0, red, 0, 0]))) + png_chunk(b"IEND", b""))


PNG = png()
# A JPEG marker fixture, deliberately not a codec-decoding fixture.
JPEG = b"\xff\xd8\xff\xe0" + b"synthetic-jpeg-marker-fixture" + b"\xff\xd9"
VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"synthetic-video-fixture"


class FakeImageMeta:
    def __init__(self, fail=None):
        self.fail = fail
        self.image_posts = self.video_posts = 0
        self.mime_types = []
        self.checked_types = None

    def preflight(self, media_types=("video",)):
        self.checked_types = set(media_types)

    def upload_image(self, path, title, mime):
        self.image_posts += 1
        self.mime_types.append(mime)
        if self.fail == "post":
            raise ApiError("API")
        return hashlib.md5(Path(path).read_bytes(), usedforsecurity=False).hexdigest()

    def image_status(self, image_hash):
        if self.fail == "read":
            raise ApiError("API", 403)
        return "READY"

    def start(self, size):
        self.video_posts += 1
        return {"video_id": "111", "upload_session_id": "222", "start_offset": "0", "end_offset": str(size)}

    def transfer(self, session, path, start, end):
        return {"start_offset": str(end), "end_offset": str(end)}

    def finish(self, session, title):
        pass

    def wait(self, video_id, seconds):
        return "READY"


class ImageTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.image = self.root / "creative.png"
        self.image.write_bytes(PNG)
        self.settings = Settings("v99.0", self.root / "state", {
            "a": Profile("a", "a", "act_100", "TOKEN_A"),
            "b": Profile("b", "b", "act_200", "TOKEN_B")}, "DRIVE_KEY", 1000000)

    def tearDown(self):
        self.tmp.cleanup()

    def job(self, job_id="one", path=None, profile="a"):
        return {"job_id": job_id, "profile": profile, "title": "same-title",
                "source": {"type": "local", "path": str(path or self.image)}}

    def run_batch(self, jobs, client):
        report, output = batch(self.settings, jobs, {"a": client, "b": client}, wait_seconds=0)
        self.assertEqual(report, json.loads(output.read_text(encoding="utf-8")))
        return report

    def config(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"api_version": "v99.0", "state_dir": "state", "profiles": {
            "a": {"product": "a", "account_id": "100", "token_env": "TOKEN_A"}}}))
        return path

    def invoke(self, command, digest, *args):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(["--config", str(self.config()), command, "--profile", "a", "--sha256", digest, *args])

    def test_directory_batch_discovers_nested_media_and_ignores_other_files(self):
        folder = self.root / "creatives"
        nested = folder / "set"
        nested.mkdir(parents=True)
        (folder / "banner.png").write_bytes(PNG)
        (nested / "clip.MP4").write_bytes(VIDEO)
        (folder / "notes.txt").write_text("ignore", encoding="utf-8")
        jobs, ignored = load_directory_jobs(folder, "a", self.settings)
        again, _ = load_directory_jobs(folder, "a", self.settings)
        self.assertEqual(ignored, 1)
        self.assertEqual(jobs, again)
        self.assertEqual([j["title"] for j in jobs], ["banner", "clip"])
        self.assertEqual(len({j["job_id"] for j in jobs}), 2)
        with contextlib.redirect_stdout(io.StringIO()) as output, patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            code = main(["--config", str(self.config()), "plan", "--directory", str(folder), "--profile", "a"])
        self.assertEqual(code, 0)
        plan = json.loads(output.getvalue())
        self.assertEqual((plan["total"], plan["ignored_files"]), (2, 1))
        self.assertEqual({j["media_type"] for j in plan["jobs"]}, {"image", "video"})

    def test_directory_upload_uses_existing_dedupe_without_network(self):
        folder = self.root / "creatives"
        folder.mkdir()
        (folder / "banner.png").write_bytes(PNG)
        client = FakeImageMeta()
        args = ["--config", str(self.config()), "upload", "--directory", str(folder), "--profile", "a", "--wait-seconds", "0"]
        with patch("meta_upload.preflight", return_value=({"a": client}, None)), contextlib.redirect_stdout(io.StringIO()) as first:
            self.assertEqual(main(args), 0)
        with patch("meta_upload.preflight", return_value=({"a": client}, None)), contextlib.redirect_stdout(io.StringIO()) as second:
            self.assertEqual(main(args), 0)
        self.assertEqual(json.loads(first.getvalue())["counts"]["ready"], 1)
        self.assertEqual(json.loads(second.getvalue())["counts"]["skipped"], 1)
        self.assertEqual(client.image_posts, 1)

    def test_directory_requires_profile_and_media(self):
        folder = self.root / "empty"
        folder.mkdir()
        with self.assertRaises(UserError):
            load_directory_jobs(folder, "a", self.settings)
        with contextlib.redirect_stderr(io.StringIO()) as errors:
            code = main(["--config", str(self.config()), "plan", "--directory", str(folder)])
        self.assertEqual(code, 2)
        self.assertIn("--profile", errors.getvalue())

    def test_png_and_jpeg_uploads_preserve_type_and_remote_hash(self):
        jpg = self.root / "photo.JPEG"
        jpg.write_bytes(JPEG)
        client = FakeImageMeta()
        report = self.run_batch([self.job(), self.job("two", jpg)], client)
        self.assertEqual(client.mime_types, ["image/png", "image/jpeg"])
        for result in report["results"]:
            self.assertEqual(result["state"], "READY")
            self.assertEqual(result["media_type"], "image")
            self.assertTrue(result["image_hash"])
            self.assertIsNone(result["video_id"])
        self.assertEqual(client.video_posts, 0)

    def test_mixed_batch_routes_video_and_image_separately(self):
        video = self.root / "video.mp4"
        video.write_bytes(VIDEO)
        client = FakeImageMeta()
        result = self.run_batch([self.job(), self.job("video", video)], client)
        self.assertEqual((client.image_posts, client.video_posts), (1, 1))
        self.assertEqual(result["counts"]["ready"], 2)
        self.assertIsNone(result["results"][1]["image_hash"])

    def test_images_dedupe_content_and_keep_all_results(self):
        client = FakeImageMeta()
        jobs = [self.job(), self.job("two")]
        first = self.run_batch(jobs, client)
        second = self.run_batch(jobs, client)
        self.assertEqual([r["state"] for r in first["results"]], ["READY", "SKIPPED"])
        self.assertEqual(second["counts"]["skipped"], 2)
        self.assertEqual(client.image_posts, 1)

    def test_same_title_changed_content_is_new_and_accounts_are_isolated(self):
        second = self.root / "other.png"
        second.write_bytes(png(0))
        client = FakeImageMeta()
        self.run_batch([self.job(), self.job("two", second), self.job("three", profile="b")], client)
        self.assertEqual(client.image_posts, 3)

    def test_image_post_timeout_never_blindly_retries(self):
        client = FakeImageMeta("post")
        for _ in range(2):
            self.assertEqual(self.run_batch([self.job()], client)["results"][0]["state"], "UNKNOWN_OUTCOME")
        self.assertEqual(client.image_posts, 1)

    def test_image_read_failure_retains_hash_and_recovers_without_post(self):
        client = FakeImageMeta("read")
        item = self.run_batch([self.job()], client)["results"][0]
        self.assertEqual(item["state"], "NEEDS_RECONCILIATION")
        self.assertTrue(item["image_hash"])
        client.fail = None
        self.assertEqual(self.run_batch([self.job()], client)["results"][0]["state"], "SKIPPED")
        self.assertEqual(client.image_posts, 1)

    def test_image_crash_intent_is_not_recreated(self):
        digest = hashlib.sha256(PNG).hexdigest()
        archive = Archive(self.settings.state_dir)
        archive.put("act_100", digest, state="STARTING", media_type="image")
        archive.close()
        client = FakeImageMeta()
        self.assertEqual(self.run_batch([self.job()], client)["results"][0]["state"], "UNKNOWN_OUTCOME")
        self.assertEqual(client.image_posts, 0)

    def test_image_preflight_requests_only_image_library(self):
        client = FakeImageMeta()
        with patch.dict(os.environ, {"TOKEN_A": "fake-token"}):
            preflight(self.settings, [self.job()], client_factory=lambda *a, **k: client)
        self.assertEqual(client.checked_types, {"image"})

    def test_offline_image_plan_and_manifest_compatibility(self):
        jobs = self.root / "jobs.json"
        jobs.write_text(json.dumps([self.job()]))
        self.assertEqual(len(load_jobs(jobs, self.settings)), 1)
        with contextlib.redirect_stdout(io.StringIO()) as output, patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            code = main(["--config", str(self.config()), "plan", "--jobs", str(jobs)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["jobs"][0]["mime_type"], "image/png")

    def test_extension_content_mismatch_is_rejected(self):
        fake = self.root / "wrong.jpg"
        fake.write_bytes(PNG)
        result = self.run_batch([self.job(path=fake)], FakeImageMeta())
        self.assertEqual(result["results"][0]["state"], "FAILED")

    def test_bad_png_crc_truncation_and_animated_png_are_rejected(self):
        animated = PNG[:33] + png_chunk(b"acTL", struct.pack(">II", 2, 0)) + PNG[33:]
        bad_crc = bytearray(PNG)
        bad_crc[-1] ^= 1
        for value in (PNG[:-2], bytes(bad_crc), animated):
            with self.subTest(value=value[:8]):
                path = self.root / "invalid.png"
                path.write_bytes(value)
                with self.assertRaises(UserError):
                    inspect_media(path, 10000)

    def test_truncated_jpeg_and_unsupported_content_are_rejected(self):
        for value in (JPEG[:-2], b"GIF89a" + b"0" * 50):
            self.image.write_bytes(value)
            with self.assertRaises(UserError):
                inspect_media(self.image, 10000)

    def test_reconcile_and_adopt_images(self):
        client = FakeImageMeta("read")
        item = self.run_batch([self.job()], client)["results"][0]
        client.fail = None
        with patch("meta_upload.make_client", return_value=client):
            self.assertEqual(self.invoke("reconcile", item["sha256"]), 0)
            other_digest = hashlib.sha256(png(0)).hexdigest()
            self.assertEqual(self.invoke("adopt", other_digest, "--image-hash", "fake_hash", "--confirm-content-match"), 0)
        archive = Archive(self.settings.state_dir)
        self.assertEqual(archive.get("act_100", other_digest)["image_hash"], "fake_hash")
        archive.close()

    def test_adopt_wrong_account_image_is_rejected(self):
        with patch("meta_upload.make_client", return_value=FakeImageMeta("read")):
            self.assertEqual(self.invoke("adopt", hashlib.sha256(PNG).hexdigest(), "--image-hash", "fake_hash", "--confirm-content-match"), 2)

    def test_drive_static_download_and_mime_spoofing(self):
        metadata = {"id": "image_file", "mimeType": "image/png", "size": str(len(PNG)),
                    "md5Checksum": hashlib.md5(PNG, usedforsecurity=False).hexdigest(),
                    "version": "1", "capabilities": {"canDownload": True}}
        class Http:
            def json(inner, *args, **kwargs):
                return metadata.copy()
            def open(inner, *args, **kwargs):
                return io.BytesIO(PNG)
        drive = DriveSource("UNUSED", Http())
        drive.token = lambda: "fake-token"
        info = drive.fetch("image_file", self.root / "download.bin", 10000)
        self.assertEqual(info["mime_type"], "image/png")
        metadata["mimeType"] = "image/jpeg"
        with self.assertRaises(UserError):
            drive.fetch("image_file", self.root / "spoofed.bin", 10000)

    def test_drive_static_batch_routes_image(self):
        class Drive:
            def fetch(inner, file_id, target, maximum):
                Path(target).write_bytes(PNG)
                return {"file_id": file_id, "version": "1", "mime_type": "image/png"}
        job = self.job()
        job["source"] = {"type": "drive", "file_id": "image_file"}
        client = FakeImageMeta()
        report, _ = batch(self.settings, [job], {"a": client}, drive=Drive(), wait_seconds=0)
        self.assertEqual(report["results"][0]["state"], "READY")
        self.assertEqual(client.image_posts, 1)

    def test_meta_image_protocol_and_secret_handling(self):
        calls = []
        class Http:
            def json(inner, method, url, token, **kwargs):
                calls.append((method, url, kwargs))
                self.assertEqual(token, "fake-token")
                self.assertTrue(url.endswith("/act_100/adimages"))
                if method == "POST":
                    body = b"".join(kwargs["body"])
                    self.assertIn(b'name="filename"', body)
                    self.assertIn(b'Content-Type: image/png', body)
                    self.assertIn(PNG, body)
                    self.assertIn(b'appsecret_proof', body)
                    self.assertNotIn(b'fake-token', body)
                    self.assertNotIn(b'fake-secret', body)
                    self.assertNotIn(b'"unsafe', body)
                    return {"images": {"image.png": {"hash": "remote_hash"}}}
                self.assertEqual(json.loads(kwargs["params"]["hashes"]), ["remote_hash"])
                return {"data": [{"hash": "remote_hash", "status": "ACTIVE"}]}
        client = MetaClient("v99.0", "fake-token", "act_100", Http(), app_secret="fake-secret")
        value = client.upload_image(self.image, '"unsafe/title', "image/png")
        self.assertEqual(client.image_status(value), "READY")
        self.assertEqual([c[0] for c in calls], ["POST", "GET"])

    def test_missing_or_ambiguous_upload_hash_is_rejected(self):
        class Http:
            def json(inner, *args, **kwargs):
                return inner.reply
        http = Http()
        client = MetaClient("v99.0", "fake", "act_100", http)
        for reply in ({}, {"images": {}}, {"images": {"a": {}}}, {"images": {"a": {"hash": "x"}, "b": {"hash": "y"}}}):
            http.reply = reply
            with self.assertRaises(UserError):
                client.upload_image(self.image, "image", "image/png")

    def test_inactive_missing_and_malformed_image_listing_are_not_ready(self):
        class Http:
            def json(inner, *args, **kwargs):
                return inner.reply
        http = Http()
        client = MetaClient("v99.0", "fake", "act_100", http)
        for reply in ({}, {"data": []}, {"data": [{"hash": "image", "status": "DELETED"}]},
                      {"data": [{"hash": "image"}]}, {"data": [], "paging": {"next": "https://untrusted.example"}}):
            http.reply = reply
            with self.assertRaises(UserError):
                client.image_status("image")


if __name__ == "__main__":
    unittest.main()

import contextlib
import hashlib
import io
import json
import os
import sqlite3
import tempfile
import unittest
import urllib.error
from pathlib import Path
from unittest.mock import patch

from common import ApiError, Http, UserError, inspect_video
from creative_archive import Archive, StateLock
from drive_fetch import DriveSource
from meta_api import MetaClient, Multipart
from meta_secrets import Profile, Settings, load_config, load_jobs
from meta_upload import batch, main, preflight


VIDEO = b"\x00\x00\x00\x18ftypmp42" + b"synthetic-test-data" * 20


class FakeMeta:
    def __init__(self, state="READY", fail_phase=None):
        self.state, self.fail_phase = state, fail_phase
        self.starts = self.finishes = self.transfers = 0
        self.size = 0

    def preflight(self, media_types=("video",)):
        pass

    def start(self, size):
        self.starts += 1
        self.size = size
        if self.fail_phase == "start":
            raise ApiError("API")
        return {"video_id": "111", "upload_session_id": "222", "start_offset": "0", "end_offset": str(size)}

    def transfer(self, session, path, start, end):
        self.transfers += 1
        if self.fail_phase == "transfer":
            raise ApiError("API")
        return {"start_offset": str(end), "end_offset": str(self.size)}

    def finish(self, session, title):
        self.finishes += 1
        if self.fail_phase == "finish":
            raise ApiError("API")

    def wait(self, video_id, seconds):
        if self.fail_phase == "status":
            raise ApiError("API")
        return self.state


class Workspace(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.video = self.root / "video.mp4"
        self.video.write_bytes(VIDEO)
        self.settings = Settings("v99.0", self.root / "state", {
            "a": Profile("a", "a", "act_100", "TOKEN_A"),
            "b": Profile("b", "b", "act_200", "TOKEN_B"),
        }, "DRIVE_CREDENTIALS", 10000000)
        self.digest = hashlib.sha256(VIDEO).hexdigest()

    def tearDown(self):
        self.tmp.cleanup()

    def job(self, name="one", path=None, profile="a", title="sample"):
        return {"job_id": name, "profile": profile, "title": title,
                "source": {"type": "local", "path": str(path or self.video)}}

    def run_batch(self, jobs, client=None):
        client = client or FakeMeta()
        result, path = batch(self.settings, jobs, {"a": client, "b": client}, wait_seconds=0)
        self.assertEqual(result, json.loads(path.read_text(encoding="utf-8")))
        return result

    def config(self):
        path = self.root / "config.json"
        path.write_text(json.dumps({"api_version": "v99.0", "state_dir": "state", "profiles": {
            "a": {"product": "a", "account_id": "100", "token_env": "TOKEN_A"}}}), encoding="utf-8")
        return path


class BatchTests(Workspace):
    def test_archive_failure_stops_and_leaves_intent_for_recovery(self):
        original = Archive.put
        def failing(archive, account, digest, **changes):
            if changes.get("state") == "TRANSFERRING":
                raise sqlite3.OperationalError("test storage fault")
            return original(archive, account, digest, **changes)
        client = FakeMeta()
        with patch.object(Archive, "put", failing), self.assertRaises(sqlite3.OperationalError):
            self.run_batch([self.job()], client)
        result = self.run_batch([self.job()], client)
        self.assertEqual(result["results"][0]["state"], "UNKNOWN_OUTCOME")
        self.assertEqual(client.starts, 1)

    def test_empty_batch_always_writes_report(self):
        result = self.run_batch([])
        self.assertEqual(result["counts"]["total"], 0)
        self.assertIsNotNone(result["finished_at"])

    def test_all_duplicates_and_trailing_duplicate_are_persisted(self):
        client = FakeMeta()
        jobs = [self.job("one"), self.job("two")]
        first = self.run_batch(jobs, client)
        second = self.run_batch(jobs, client)
        self.assertEqual(client.starts, 1)
        self.assertEqual([r["state"] for r in first["results"]], ["READY", "SKIPPED"])
        self.assertEqual([r["state"] for r in second["results"]], ["SKIPPED", "SKIPPED"])
        self.assertEqual(second["counts"]["processed"], 2)

    def test_same_title_different_bytes_upload_separately(self):
        other = self.root / "other.mp4"
        other.write_bytes(VIDEO + b"new-content")
        client = FakeMeta()
        self.run_batch([self.job(), self.job("two", other)], client)
        self.assertEqual(client.starts, 2)

    def test_same_bytes_different_accounts_upload_separately(self):
        client = FakeMeta()
        self.run_batch([self.job(), self.job("two", profile="b")], client)
        self.assertEqual(client.starts, 2)

    def test_unknown_write_outcomes_are_not_retried(self):
        for phase in ("start", "transfer", "finish"):
            with self.subTest(phase=phase):
                self.settings = Settings("v99.0", self.root / phase, self.settings.profiles, "DRIVE", 10000000)
                client = FakeMeta(fail_phase=phase)
                first = self.run_batch([self.job()], client)
                second = self.run_batch([self.job()], client)
                self.assertEqual(first["results"][0]["state"], "UNKNOWN_OUTCOME")
                self.assertEqual(second["results"][0]["state"], "UNKNOWN_OUTCOME")
                self.assertEqual(client.starts, 1)

    def test_processing_is_not_success_and_does_not_upload_again(self):
        client = FakeMeta(state="PROCESSING")
        self.assertEqual(self.run_batch([self.job()], client)["counts"]["unresolved"], 1)
        client.state = "READY"
        self.assertEqual(self.run_batch([self.job()], client)["results"][0]["state"], "SKIPPED")
        self.assertEqual(client.starts, 1)

    def test_processing_failure_does_not_auto_reupload(self):
        client = FakeMeta(state="PROCESSING_FAILED")
        for _ in range(2):
            self.assertEqual(self.run_batch([self.job()], client)["results"][0]["state"], "PROCESSING_FAILED")
        self.assertEqual(client.starts, 1)

    def test_status_read_error_retains_remote_id_and_does_not_upload_again(self):
        client = FakeMeta(fail_phase="status")
        result = self.run_batch([self.job()], client)["results"][0]
        self.assertEqual(result["state"], "NEEDS_RECONCILIATION")
        self.assertEqual(result["video_id"], "111")
        client.fail_phase = None
        self.assertEqual(self.run_batch([self.job()], client)["results"][0]["state"], "SKIPPED")
        self.assertEqual(client.starts, 1)

    def test_crash_intent_blocks_recreation(self):
        archive = Archive(self.settings.state_dir)
        archive.put("act_100", self.digest, state="STARTING")
        archive.close()
        client = FakeMeta()
        self.assertEqual(self.run_batch([self.job()], client)["results"][0]["state"], "UNKNOWN_OUTCOME")
        self.assertEqual(client.starts, 0)

    def test_local_missing_file_preserves_report_and_continues(self):
        result = self.run_batch([self.job(path=self.root / "missing.mp4"), self.job("two")])
        self.assertEqual([r["state"] for r in result["results"]], ["FAILED", "READY"])

    def test_source_failure_continues_other_jobs(self):
        bad = self.root / "bad.mp4"
        bad.write_bytes(b"not mp4")
        result = self.run_batch([self.job("bad", bad), self.job("good")])
        self.assertEqual([r["state"] for r in result["results"]], ["FAILED", "READY"])
        self.assertEqual(result["counts"]["processed"], 2)

    def test_no_path_or_title_becomes_staging_filename(self):
        result = self.run_batch([self.job(title="../../display-title")])
        self.assertEqual(result["results"][0]["state"], "READY")
        self.assertFalse((self.root / "display-title").exists())

    def test_atomic_registry_and_audit(self):
        archive = Archive(self.settings.state_dir)
        archive.put("act_100", self.digest, state="STARTING")
        archive.put("act_100", self.digest, state="UNKNOWN_OUTCOME")
        self.assertEqual(len(archive.all()), 1)
        self.assertEqual(archive.db.execute("SELECT count(*) FROM events").fetchone()[0], 2)
        archive.close()

    def test_second_state_lock_fails(self):
        with StateLock(self.settings.state_dir):
            with self.assertRaises(UserError):
                with StateLock(self.settings.state_dir):
                    self.fail("Lock allowed concurrent writer")
        with StateLock(self.settings.state_dir):
            pass


class InputTests(Workspace):
    def test_independent_profile_credentials(self):
        seen = []
        def factory(version, token, account):
            seen.append((token, account))
            return FakeMeta()
        with patch.dict(os.environ, {"TOKEN_A": "secret-a", "TOKEN_B": "secret-b"}):
            preflight(self.settings, [self.job(), self.job("two", profile="b")], factory)
        self.assertEqual(seen, [("secret-a", "act_100"), ("secret-b", "act_200")])

    def test_offline_plan_requires_no_credentials_or_network(self):
        manifest = self.root / "jobs.json"
        manifest.write_text(json.dumps([self.job()]), encoding="utf-8")
        with contextlib.redirect_stdout(io.StringIO()) as output, patch("socket.socket", side_effect=AssertionError("Network forbidden")):
            code = main(["--config", str(self.config()), "plan", "--jobs", str(manifest)])
        self.assertEqual(code, 0)
        self.assertEqual(json.loads(output.getvalue())["mode"], "offline_plan")
        self.assertFalse(self.settings.state_dir.exists())

    def test_invalid_or_unknown_config_fields_rejected(self):
        path = self.config()
        raw = json.loads(path.read_text())
        raw["token"] = "should-never-be-stored-here"
        path.write_text(json.dumps(raw))
        with self.assertRaises(UserError) as error:
            load_config(path)
        self.assertNotIn("should-never", str(error.exception))

    def test_misplaced_credential_is_never_echoed(self):
        fake_credential = "EAA" + "x" * 96
        for field in ("token_env", "app_secret_env"):
            with self.subTest(field=field):
                path = self.config()
                raw = json.loads(path.read_text(encoding="utf-8"))
                raw["profiles"]["a"][field] = fake_credential
                path.write_text(json.dumps(raw), encoding="utf-8")
                with contextlib.redirect_stderr(io.StringIO()) as errors:
                    code = main(["--config", str(path), "status"])
                self.assertEqual(code, 2)
                self.assertNotIn(fake_credential, errors.getvalue())
        for key in ("token_env", "app_secret_env"):
            profile = Profile("a", "a", "act_100", "EAA" + "x" * 20,
                              "EAA" + "x" * 20 if key == "app_secret_env" else None)
            with patch.dict(os.environ, {}, clear=True), self.assertRaises(UserError) as error:
                profile.token() if key == "token_env" else profile.app_secret()
            self.assertNotIn("EAA", str(error.exception))

    def test_duplicate_jobs_unknown_profile_and_unsafe_id_rejected(self):
        cases = [[self.job(), self.job()], [self.job(profile="missing")], [self.job(name="../outside")]]
        for data in cases:
            path = self.root / "jobs.json"
            path.write_text(json.dumps(data))
            with self.assertRaises(UserError):
                load_jobs(path, self.settings)

    def test_relative_source_paths_use_manifest_location(self):
        path = self.root / "jobs.json"
        item = self.job()
        item["source"]["path"] = "video.mp4"
        path.write_text(json.dumps([item]))
        self.assertEqual(load_jobs(path, self.settings)[0]["source"]["path"], str(self.video.resolve()))

    def test_header_and_size_validation(self):
        self.assertEqual(inspect_video(self.video, 10000)[0], self.digest)
        with self.assertRaises(UserError):
            inspect_video(self.video, 2)

    def test_partial_upload_cli_returns_nonzero(self):
        path = self.root / "jobs.json"
        path.write_text(json.dumps([self.job()]))
        with patch("meta_upload.preflight", return_value=({"a": FakeMeta(state="PROCESSING")}, None)), contextlib.redirect_stdout(io.StringIO()):
            code = main(["--config", str(self.config()), "upload", "--jobs", str(path), "--wait-seconds", "0"])
        self.assertEqual(code, 1)


class TransportTests(Workspace):
    def test_meta_adapter_end_to_end_protocol_with_fake_http(self):
        class Protocol:
            phases = []
            def json(inner, method, url, token, params=None, body=None, headers=None):
                self.assertEqual(token, "test-token")
                self.assertTrue(url.startswith("https://graph"))
                if body is not None:
                    payload = b"".join(body)
                    self.assertIn(VIDEO, payload)
                    self.assertEqual(len(payload), len(body))
                    inner.phases.append("transfer")
                    return {"start_offset": str(len(VIDEO)), "end_offset": str(len(VIDEO))}
                phase = params.get("upload_phase")
                if phase:
                    inner.phases.append(phase)
                if phase == "start":
                    return {"video_id": "111", "upload_session_id": "222", "start_offset": "0", "end_offset": str(len(VIDEO))}
                if phase == "finish":
                    return {"success": True}
                return {"status": {"video_status": "ready"}}
        protocol = Protocol()
        result = self.run_batch([self.job()], MetaClient("v99.0", "test-token", "act_100", protocol))
        self.assertEqual(result["results"][0]["state"], "READY")
        self.assertEqual(protocol.phases, ["start", "transfer", "finish"])

    def test_app_secret_proof_for_get_and_multipart(self):
        class Capture:
            calls = []
            def json(inner, *args, **kwargs):
                inner.calls.append(kwargs)
                return {"status": {"video_status": "ready"}}
        http = Capture()
        client = MetaClient("v99.0", "token", "act_100", http, app_secret="app-secret")
        self.assertEqual(client.status("111"), "READY")
        self.assertEqual(len(http.calls[0]["params"]["appsecret_proof"]), 64)
        client.transfer("222", self.video, 0, len(VIDEO))
        body = b"".join(http.calls[1]["body"])
        self.assertIn(b'appsecret_proof', body)
        self.assertNotIn(b'app-secret', body)

    def test_invalid_processing_response_is_not_silently_pending(self):
        class Invalid:
            def json(inner, *args, **kwargs):
                return {"status": None}
        with self.assertRaises(UserError):
            MetaClient("v99.0", "token", "act_100", Invalid()).status("111")

    def test_streamed_multipart_content_length_and_slice(self):
        body = Multipart({"upload_phase": "transfer", "start_offset": 3}, self.video, 3, 19)
        parts = list(body)
        joined = b"".join(parts)
        self.assertEqual(len(joined), len(body))
        self.assertEqual(parts[1], VIDEO[3:19])
        self.assertNotIn(str(self.video).encode(), joined)

    def test_http_token_is_header_only_and_post_not_retried(self):
        http = Http()
        class Broken:
            def __init__(self):
                self.requests = []
            def open(inner, request, **kwargs):
                inner.requests.append(request)
                raise urllib.error.URLError("secret-token in private-url")
        broken = Broken()
        http.opener = broken
        with self.assertRaises(UserError) as error:
            http.json("POST", "https://graph.facebook.com/v99.0/act_100/advideos", "secret-token", params={"upload_phase": "start"})
        self.assertEqual(len(broken.requests), 1)
        request = broken.requests[0]
        self.assertEqual(request.get_header("Authorization"), "Bearer secret-token")
        self.assertNotIn("secret-token", request.full_url)
        self.assertNotIn(b"secret-token", request.data)
        self.assertNotIn("secret-token", str(error.exception))

    def test_read_retry_bounded(self):
        http = Http()
        with patch.object(http.opener, "open", side_effect=urllib.error.URLError("private")) as call, patch("common.time.sleep"):
            with self.assertRaises(UserError):
                http.json("GET", "https://graph.facebook.com", "secret")
        self.assertEqual(call.call_count, 3)

    def test_listing_does_not_follow_remote_next_url_or_assume_complete(self):
        class Listing:
            def json(inner, *args, **kwargs):
                return {"data": [], "paging": {"next": "https://untrusted.example/?access_token=secret"}}
        client = MetaClient("v99.0", "secret", "act_100", Listing())
        with self.assertRaises(UserError):
            client.owns("111")


class DriveTests(Workspace):
    def drive(self, payload=VIDEO, mutate=False):
        metadata = {"id": "file_one", "mimeType": "video/mp4", "size": str(len(VIDEO)),
                    "md5Checksum": hashlib.md5(VIDEO, usedforsecurity=False).hexdigest(),
                    "version": "1", "capabilities": {"canDownload": True}}
        class FakeHttp:
            calls = 0
            def json(inner, *args, **kwargs):
                inner.calls += 1
                return {**metadata, "version": "2" if mutate and inner.calls > 1 else "1"}
            def open(inner, *args, **kwargs):
                return io.BytesIO(payload)
        source = DriveSource("UNUSED", FakeHttp())
        source.token = lambda: "test-token"
        return source

    def test_drive_download_validates_content(self):
        target = self.root / "download.mp4"
        revision = self.drive().fetch("file_one", target, 10000)
        self.assertEqual(target.read_bytes(), VIDEO)
        self.assertEqual(revision["version"], "1")

    def test_drive_changed_source_or_partial_content_rejected(self):
        for i, source in enumerate([self.drive(payload=VIDEO[:-1]), self.drive(mutate=True)]):
            with self.assertRaises(UserError):
                source.fetch("file_one", self.root / f"download-{i}.mp4", 10000)


class RecoveryTests(Workspace):
    def test_deleted_or_unreadable_known_object_requires_explicit_resolution(self):
        self.record("READY")
        class Unreadable:
            def status(self, video_id):
                raise ApiError("API", 404)
        with patch("meta_upload.make_client", return_value=Unreadable()):
            self.assertEqual(self.invoke("reconcile"), 2)
        self.assertEqual(self.state(), "NEEDS_RECONCILIATION")
        self.assertEqual(self.invoke("reset", "--confirm-no-remote-object"), 0)

    def invoke(self, command, *extra):
        with contextlib.redirect_stdout(io.StringIO()), contextlib.redirect_stderr(io.StringIO()):
            return main(["--config", str(self.config()), command, "--profile", "a", "--sha256", self.digest, *extra])

    def record(self, state, video_id="111"):
        archive = Archive(self.settings.state_dir)
        archive.put("act_100", self.digest, state=state, video_id=video_id)
        archive.close()

    def state(self):
        archive = Archive(self.settings.state_dir)
        try:
            return archive.get("act_100", self.digest)["state"]
        finally:
            archive.close()

    def test_reconcile_unknown_processing_does_not_confirm_finish(self):
        self.record("UNKNOWN_OUTCOME")
        class Processing:
            def status(self, video_id):
                return "PROCESSING"
        with patch("meta_upload.make_client", return_value=Processing()):
            self.assertEqual(self.invoke("reconcile"), 1)
        self.assertEqual(self.state(), "UNKNOWN_OUTCOME")

    def test_reconcile_ready_resolves_uncertainty(self):
        self.record("UNKNOWN_OUTCOME")
        class Ready:
            def status(self, video_id):
                return "READY"
        with patch("meta_upload.make_client", return_value=Ready()):
            self.assertEqual(self.invoke("reconcile"), 0)
        self.assertEqual(self.state(), "READY")

    def test_adopt_checks_account_membership(self):
        class WrongAccount:
            def owns(self, video_id):
                return False
        with patch("meta_upload.make_client", return_value=WrongAccount()):
            self.assertEqual(self.invoke("adopt", "--video-id", "111", "--confirm-content-match"), 2)
        archive = Archive(self.settings.state_dir)
        self.assertIsNone(archive.get("act_100", self.digest))
        archive.close()

    def test_reset_rejects_ready_and_records_explicit_recovery(self):
        self.record("READY")
        self.assertEqual(self.invoke("reset", "--confirm-no-remote-object"), 2)
        self.record("UNKNOWN_OUTCOME")
        self.assertEqual(self.invoke("reset", "--confirm-no-remote-object"), 0)
        self.assertEqual(self.state(), "RETRY_ALLOWED")


if __name__ == "__main__":
    unittest.main()

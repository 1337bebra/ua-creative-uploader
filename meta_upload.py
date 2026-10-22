"""Batch video/static-image delivery; importing this module performs no work."""
from __future__ import annotations

import argparse
import json
import re
import sys
import tempfile
import uuid
from pathlib import Path

from common import UserError, atomic_json, identifier, inspect_media, mime_for_path, utcnow
from creative_archive import Archive, StateLock
from drive_fetch import DriveSource
from meta_api import MetaClient, remote_id
from meta_secrets import load_config, load_directory_jobs, load_jobs


def error_message(error):
    return str(error) if isinstance(error, UserError) else f"Local operation failed ({type(error).__name__})"


def offsets(response, size, minimum=0):
    try:
        start, end = int(response["start_offset"]), int(response["end_offset"])
    except (KeyError, TypeError, ValueError):
        raise UserError("Meta returned invalid transfer offsets") from None
    if not 0 <= minimum <= start <= end <= size or (start == end and end != size):
        raise UserError("Meta returned inconsistent transfer offsets")
    return start, end


def upload_new(client, archive, account, digest, path, size, title, run_id):
    """Every remote mutation has a durable intent. Uncertain writes never auto-retry."""
    archive.put(account, digest, state="STARTING", run_id=run_id, title=title, size=size,
                media_type="video", mime_type="video/mp4", video_id=None, image_hash=None, session_id=None, error=None)
    try:
        reply = client.start(size)
        video_id, session = remote_id(reply.get("video_id")), remote_id(reply.get("upload_session_id"))
        archive.put(account, digest, state="TRANSFERRING", video_id=video_id, session_id=session)
        start, end = offsets(reply, size)
        while start < end:
            archive.put(account, digest, state="TRANSFERRING", start_offset=start, end_offset=end)
            reply = client.transfer(session, path, start, end)
            next_start, next_end = offsets(reply, size, minimum=start + 1)
            if next_start != end:
                raise UserError("Meta acknowledged an unexpected transfer offset")
            start, end = next_start, next_end
        archive.put(account, digest, state="FINISHING", start_offset=start, end_offset=end)
        client.finish(session, title)
        return archive.put(account, digest, state="PROCESSING", error=None)
    except UserError as error:
        # A failed request/invalid response may still have changed remote state.
        return archive.put(account, digest, state="UNKNOWN_OUTCOME", error=error_message(error))
    # Unexpected local failures intentionally escape: durable intent still blocks resubmission.


def upload_image_new(client, archive, account, media, path, title, run_id):
    digest = media["sha256"]
    archive.put(account, digest, state="STARTING", run_id=run_id, title=title,
                size=media["size"], media_type="image", mime_type=media["mime_type"],
                video_id=None, image_hash=None, session_id=None, error=None)
    try:
        image_hash = client.upload_image(path, title, media["mime_type"])
        identifier(image_hash, "Meta image hash")
        return archive.put(account, digest, state="UPLOADED", image_hash=image_hash, error=None)
    except UserError as error:
        return archive.put(account, digest, state="UNKNOWN_OUTCOME", error=error_message(error))


def asset_status(client, asset, wait_seconds=0):
    if asset.get("media_type", "video") == "image":
        if not asset.get("image_hash"):
            raise UserError("No image hash is stored; inspect the library, then adopt or reset")
        return client.image_status(asset["image_hash"])
    if not asset.get("video_id"):
        raise UserError("No video ID is stored; inspect the library, then adopt or reset")
    return client.wait(asset["video_id"], wait_seconds)


def snapshot_local(source, target, max_bytes):
    total = 0
    with Path(source).open("rb") as incoming, Path(target).open("xb") as output:
        while block := incoming.read(1024 * 1024):
            total += len(block)
            if total > max_bytes:
                raise UserError("Local file exceeds max_file_bytes")
            output.write(block)


def make_client(settings, profile, client_factory=MetaClient):
    secret = profile.app_secret()
    return client_factory(settings.api_version, profile.token(), profile.account,
                          **({"app_secret": secret} if secret else {}))


def preflight(settings, jobs, client_factory=MetaClient, drive_factory=DriveSource):
    media_types = {}
    drive = drive_factory(settings.drive_credentials_env) if any(j["source"]["type"] == "drive" for j in jobs) else None
    for job in jobs:
        if job["source"]["type"] == "local":
            path = job["source"]["path"]
            kind = inspect_media(path, settings.max_file_bytes, mime_for_path(path))["media_type"]
        else:
            metadata = drive.check(job["source"]["file_id"], settings.max_file_bytes)
            kind = "image" if metadata["mimeType"].startswith("image/") else "video"
        media_types.setdefault(job["profile"], set()).add(kind)
    clients = {}
    for name in sorted({job["profile"] for job in jobs}):
        profile = settings.profiles[name]
        client = make_client(settings, profile, client_factory)
        client.preflight(media_types=media_types[name])
        clients[name] = client
    return clients, drive


def batch(settings, jobs, clients, drive=None, wait_seconds=30):
    run_id = uuid.uuid4().hex
    results = [{"job_id": j["job_id"], "profile": j["profile"], "product": settings.profiles[j["profile"]].product,
                "account": settings.profiles[j["profile"]].account, "title": j["title"], "state": "PENDING"} for j in jobs]
    output = settings.state_dir / "runs" / run_id / "results.json"
    report = {"run_id": run_id, "started_at": utcnow(), "finished_at": None, "results": results}

    def save():
        report["counts"] = {"total": len(results), "processed": sum(r["state"] != "PENDING" for r in results),
                            "ready": sum(r["state"] == "READY" for r in results),
                            "skipped": sum(r["state"] == "SKIPPED" for r in results),
                            "unresolved": sum(r["state"] not in {"READY", "SKIPPED", "PENDING"} for r in results)}
        atomic_json(output, report)

    with StateLock(settings.state_dir):
        archive = Archive(settings.state_dir)
        try:
            save()
            staging = settings.state_dir / "staging"
            staging.mkdir(parents=True, exist_ok=True)
            for job, result in zip(jobs, results):
                profile, client = settings.profiles[job["profile"]], clients[job["profile"]]
                digest = None
                try:
                    with tempfile.TemporaryDirectory(prefix="asset-", dir=staging) as folder:
                        video = Path(folder) / "asset.bin"
                        source = job["source"]
                        try:
                            if source["type"] == "local":
                                snapshot_local(source["path"], video, settings.max_file_bytes)
                                expected_mime = mime_for_path(source["path"])
                            else:
                                if drive is None:
                                    raise UserError("Drive source is not configured")
                                result["source_revision"] = drive.fetch(source["file_id"], video, settings.max_file_bytes)
                                expected_mime = result["source_revision"]["mime_type"]
                            media = inspect_media(video, settings.max_file_bytes, expected_mime)
                            digest, size = media["sha256"], media["size"]
                        except OSError as error:
                            raise UserError(f"Cannot prepare source file ({type(error).__name__})") from None
                        result.update(media)
                        previous = archive.get(profile.account, digest)
                        reused = bool(previous and previous["state"] != "RETRY_ALLOWED")
                        if reused:
                            asset = previous
                            if asset.get("media_type", "video") != media["media_type"]:
                                raise UserError("Registry media type conflicts with source content")
                            if asset["state"] in {"STARTING", "TRANSFERRING", "FINISHING"}:
                                asset = archive.put(profile.account, digest, state="UNKNOWN_OUTCOME",
                                                    error="Interrupted write; reconcile before another upload")
                        else:
                            if media["media_type"] == "image":
                                asset = upload_image_new(client, archive, profile.account, media, video, job["title"], run_id)
                            else:
                                asset = upload_new(client, archive, profile.account, digest, video, size, job["title"], run_id)
                        if asset["state"] in {"READY", "UPLOADED", "PROCESSING", "NEEDS_RECONCILIATION"}:
                            # Revalidate old mappings rather than skipping a remotely removed object.
                            state = asset_status(client, asset, wait_seconds)
                            asset = archive.put(profile.account, digest, state=state, error=None)
                        result.update(state="SKIPPED" if reused and asset["state"] == "READY" else asset["state"],
                                      video_id=asset.get("video_id"), image_hash=asset.get("image_hash"), reused=reused)
                        if asset.get("error"):
                            result["error"] = asset["error"]
                except UserError as error:
                    result.update(state="FAILED", error=error_message(error))
                    if digest:
                        known = archive.get(profile.account, digest)
                        if known:
                            if known["state"] in {"READY", "UPLOADED", "PROCESSING", "NEEDS_RECONCILIATION"}:
                                known = archive.put(profile.account, digest, state="NEEDS_RECONCILIATION",
                                                    error=error_message(error))
                            result.update(state="NEEDS_RECONCILIATION", video_id=known.get("video_id"),
                                          image_hash=known.get("image_hash"), registry_state=known["state"])
                except BaseException as error:
                    result.update(state="INTERRUPTED", error=error_message(error))
                    save()
                    raise
                save()
            report["finished_at"] = utcnow()
            save()
        finally:
            archive.close()
    return report, output


def parser():
    cli = argparse.ArgumentParser(description="Upload MP4, JPG and PNG creatives to Meta media libraries")
    cli.add_argument("--config", default="config.json")
    sub = cli.add_subparsers(dest="command", required=True)
    for name in ("plan", "check", "upload"):
        command = sub.add_parser(name)
        source = command.add_mutually_exclusive_group(required=True)
        source.add_argument("--jobs")
        source.add_argument("--directory")
        command.add_argument("--profile", help="Required with --directory; selects its account")
        if name == "upload":
            command.add_argument("--wait-seconds", type=int, default=30)
    sub.add_parser("status")
    for name in ("reconcile", "adopt", "reset"):
        command = sub.add_parser(name)
        command.add_argument("--profile", required=True)
        command.add_argument("--sha256", required=True)
        if name == "adopt":
            remote = command.add_mutually_exclusive_group(required=True)
            remote.add_argument("--video-id")
            remote.add_argument("--image-hash")
            command.add_argument("--confirm-content-match", action="store_true", required=True)
        if name == "reset":
            command.add_argument("--confirm-no-remote-object", action="store_true", required=True)
    return cli


def main(argv=None):
    args = parser().parse_args(argv)
    try:
        settings = load_config(args.config)
        if args.command in {"plan", "check", "upload"}:
            if args.directory:
                if not args.profile:
                    raise UserError("--profile is required with --directory")
                jobs, ignored = load_directory_jobs(args.directory, args.profile, settings)
            else:
                if args.profile:
                    raise UserError("--profile is only used with --directory; jobs select their own profiles")
                jobs, ignored = load_jobs(args.jobs, settings), 0
            if args.command == "plan":
                plan = []
                for job in jobs:
                    profile = settings.profiles[job["profile"]]
                    item = {"job_id": job["job_id"], "profile": profile.name, "product": profile.product,
                            "account": profile.account, "title": job["title"], "source_type": job["source"]["type"]}
                    if job["source"]["type"] == "local":
                        path = job["source"]["path"]
                        item.update(inspect_media(path, settings.max_file_bytes, mime_for_path(path)))
                    plan.append(item)
                print(json.dumps({"mode": "offline_plan", "total": len(plan), "ignored_files": ignored,
                                  "jobs": plan}, ensure_ascii=False, indent=2))
                return 0
            if args.command == "upload" and not 0 <= args.wait_seconds <= 3600:
                raise UserError("wait-seconds must be between 0 and 3600")
            clients, drive = preflight(settings, jobs)
            if args.command == "check":
                print(f"Read access and {len(jobs)} sources checked ({ignored} unsupported files ignored). "
                      "Upload permission and encoding still require an actual upload.")
                return 0
            report, output = batch(settings, jobs, clients, drive, args.wait_seconds)
            print(json.dumps({"run_id": report["run_id"], "counts": report["counts"],
                              "ignored_files": ignored, "results_file": str(output)}, indent=2))
            return 1 if report["counts"]["unresolved"] else 0
        with StateLock(settings.state_dir):
            archive = Archive(settings.state_dir)
            try:
                if args.command == "status":
                    print(json.dumps(archive.all(), ensure_ascii=False, indent=2))
                    return 0
                if args.profile not in settings.profiles or not re.fullmatch(r"[0-9a-f]{64}", args.sha256):
                    raise UserError("Select an existing profile and a lowercase SHA-256 digest")
                profile = settings.profiles[args.profile]
                old = archive.get(profile.account, args.sha256)
                if args.command == "reset":
                    if not old or old["state"] not in {"UNKNOWN_OUTCOME", "STARTING", "TRANSFERRING", "FINISHING", "PROCESSING_FAILED", "NEEDS_RECONCILIATION"}:
                        raise UserError("Reset is limited to uncertain/failed uploads; inspect the registry first")
                    asset = archive.put(profile.account, args.sha256, state="RETRY_ALLOWED", video_id=None, image_hash=None,
                                        session_id=None, error=None, resolution="operator_confirmed_no_remote_object")
                else:
                    client = make_client(settings, profile)
                    if args.command == "adopt":
                        if old and old["state"] in {"READY", "UPLOADED", "PROCESSING"}:
                            raise UserError("An active mapping already exists")
                        kind = "image" if args.image_hash else "video"
                        if old and old.get("media_type", "video") != kind:
                            raise UserError("Cannot adopt a different media type for this registry entry")
                        if kind == "image":
                            identifier(args.image_hash, "Meta image hash")
                            state = client.image_status(args.image_hash)
                        else:
                            remote_id(args.video_id)
                            if not client.owns(args.video_id):
                                raise UserError("Video was not found in the selected account")
                            state = client.status(args.video_id)
                        if state != "READY":
                            raise UserError("Only READY assets can be adopted")
                        asset = archive.put(profile.account, args.sha256, media_type=kind,
                                            video_id=args.video_id, image_hash=args.image_hash, state=state,
                                            error=None, resolution="operator_confirmed_content_match")
                    else:
                        if not old:
                            raise UserError("No registry entry; inspect the media library, then adopt")
                        try:
                            if old.get("media_type", "video") == "image":
                                state = asset_status(client, old)
                            elif old.get("video_id"):
                                state = client.status(old["video_id"])
                            else:
                                raise UserError("No video ID is stored; inspect the media library, then adopt or reset")
                        except UserError as error:
                            if old["state"] in {"READY", "UPLOADED", "PROCESSING", "NEEDS_RECONCILIATION"}:
                                archive.put(profile.account, args.sha256, state="NEEDS_RECONCILIATION",
                                            error=error_message(error))
                            raise
                        # An unfinished session reporting processing is not proof that finish succeeded.
                        if old["state"] in {"UNKNOWN_OUTCOME", "STARTING", "TRANSFERRING", "FINISHING"} and state == "PROCESSING":
                            state = "UNKNOWN_OUTCOME"
                        asset = archive.put(profile.account, args.sha256, state=state,
                                            error=None if state == "READY" else old.get("error"))
                print(json.dumps(asset, ensure_ascii=False, indent=2))
                return 0 if asset["state"] in {"READY", "RETRY_ALLOWED"} else 1
            finally:
                archive.close()
    except KeyboardInterrupt:
        print("Interrupted. Inspect status before retrying.", file=sys.stderr)
        return 130
    except Exception as error:
        print(error_message(error), file=sys.stderr)
        return 2


if __name__ == "__main__":
    sys.exit(main())

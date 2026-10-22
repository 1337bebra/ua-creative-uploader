"""Configuration and credential references. Never put actual secrets in this file."""
from __future__ import annotations

import os
import re
import hashlib
from dataclasses import dataclass, field
from pathlib import Path

from common import MEDIA_MIMES, UserError, identifier, read_json, require_keys


@dataclass(frozen=True)
class Profile:
    name: str
    product: str
    account: str
    token_env: str | None
    app_secret_env: str | None = None
    token_value: str | None = field(default=None, repr=False)

    def token(self):
        value = self.token_value if self.token_value is not None else os.environ.get(self.token_env or "", "")
        if not value or value != value.strip() or any(ord(ch) < 33 for ch in value):
            raise UserError("Set a valid Meta token in this profile's token field or token_env variable")
        return value

    def app_secret(self):
        if self.app_secret_env is None:
            return None
        value = os.environ.get(self.app_secret_env, "").strip()
        if not value or any(ord(ch) < 32 for ch in value):
            raise UserError("Set a valid app secret in the environment variable named by app_secret_env")
        return value


@dataclass(frozen=True)
class Settings:
    api_version: str
    state_dir: Path
    profiles: dict[str, Profile]
    drive_credentials_env: str
    max_file_bytes: int


def load_config(path):
    path = Path(path).resolve()
    raw = read_json(path)
    require_keys(raw, {"api_version", "state_dir", "profiles", "drive_credentials_env", "max_file_bytes"}, "config")
    version = raw.get("api_version")
    if not isinstance(version, str) or not re.fullmatch(r"v[1-9][0-9]*\.0", version):
        raise UserError("Set api_version to a supported Meta Graph version, for example vNN.0 with NN replaced")
    if not isinstance(raw.get("profiles"), dict) or not raw["profiles"]:
        raise UserError("config.profiles must be a nonempty object")
    profiles = {}
    for name, item in raw["profiles"].items():
        identifier(name, "profile name")
        require_keys(item, {"product", "account_id", "token_env", "token", "app_secret_env"}, "profile")
        account = str(item.get("account_id", ""))
        if not re.fullmatch(r"(?:act_)?[0-9]+", account):
            raise UserError("Each profile needs a numeric account_id, optionally prefixed with act_")
        account = account if account.startswith("act_") else "act_" + account
        if ("token" in item) == ("token_env" in item):
            raise UserError("Each profile needs exactly one of token or token_env")
        token_env = item.get("token_env")
        token_value = item.get("token")
        if "token_env" in item and (not isinstance(token_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", token_env)):
            raise UserError("token_env must contain a short environment-variable name, not a token value")
        if "token" in item and (not isinstance(token_value, str) or len(token_value) > 4096):
            raise UserError("token must be a string of at most 4096 characters")
        product = identifier(item.get("product"), "product")
        app_secret_env = item.get("app_secret_env")
        if app_secret_env is not None and (not isinstance(app_secret_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]{0,63}", app_secret_env)):
            raise UserError("app_secret_env must contain a short environment-variable name, not a secret value")
        profiles[name] = Profile(name, product, account, token_env, app_secret_env, token_value)
    limit = raw.get("max_file_bytes", 2 * 1024 ** 3)
    if type(limit) is not int or limit < 1:
        raise UserError("max_file_bytes must be a positive integer")
    location = raw.get("state_dir", "state")
    if not isinstance(location, str) or not location:
        raise UserError("state_dir must be a directory path")
    credential_env = raw.get("drive_credentials_env", "GOOGLE_APPLICATION_CREDENTIALS")
    if not isinstance(credential_env, str) or not re.fullmatch(r"[A-Za-z_][A-Za-z0-9_]*", credential_env):
        raise UserError("drive_credentials_env must name an environment variable")
    return Settings(version, (path.parent / location).resolve(), profiles, credential_env, limit)


def load_jobs(path, settings):
    path = Path(path).resolve()
    raw = read_json(path)
    if not isinstance(raw, list):
        raise UserError("Jobs must be a JSON array")
    jobs, seen = [], set()
    for item in raw:
        require_keys(item, {"job_id", "profile", "title", "source"}, "job")
        job_id = identifier(item.get("job_id"), "job_id")
        if job_id in seen:
            raise UserError("job_id values must be unique within a batch")
        seen.add(job_id)
        profile = item.get("profile")
        if not isinstance(profile, str) or profile not in settings.profiles:
            raise UserError("Each job must select an existing profile")
        title = item.get("title")
        if not isinstance(title, str) or not title.strip() or len(title) > 200 or any(ord(c) < 32 for c in title):
            raise UserError("title must contain 1-200 characters without control characters")
        source = item.get("source")
        require_keys(source, {"type", "path", "file_id"}, "source")
        if source.get("type") == "local" and set(source) == {"type", "path"}:
            if not isinstance(source["path"], str) or not source["path"]:
                raise UserError("Local source requires a path")
            local = (path.parent / source["path"]).resolve()
            if not local.is_file() or local.suffix.lower() not in MEDIA_MIMES:
                raise UserError(f"Job {job_id}: local source must be an existing .mp4, .jpg, .jpeg or .png file")
            inspect_source = {"type": "local", "path": str(local)}
        elif source.get("type") == "drive" and set(source) == {"type", "file_id"}:
            inspect_source = {"type": "drive", "file_id": identifier(source["file_id"], "Drive file ID")}
        else:
            raise UserError("source must specify either local/path or drive/file_id")
        jobs.append({"job_id": job_id, "profile": profile, "title": title.strip(), "source": inspect_source})
    return jobs


def load_directory_jobs(directory, profile, settings):
    if profile not in settings.profiles:
        raise UserError("Select an existing profile with --profile")
    root = Path(directory).resolve()
    if not root.is_dir():
        raise UserError("--directory must point to an existing folder")
    files = sorted((p for p in root.rglob("*") if p.is_file() and not p.is_symlink()),
                   key=lambda p: (p.relative_to(root).as_posix().casefold(), p.relative_to(root).as_posix()))
    jobs, ignored = [], 0
    for path in files:
        if path.suffix.lower() not in MEDIA_MIMES:
            ignored += 1
            continue
        relative = path.relative_to(root).as_posix()
        title = path.stem.strip()
        if not title or len(title) > 200 or any(ord(ch) < 32 for ch in title):
            raise UserError("Each media filename must provide a title of 1-200 characters without control characters")
        job_id = "file_" + hashlib.sha256(relative.encode("utf-8")).hexdigest()[:24]
        jobs.append({"job_id": job_id, "profile": profile, "title": title,
                     "source": {"type": "local", "path": str(path)}})
    if not jobs:
        raise UserError("No MP4, JPG, JPEG or PNG files found in --directory")
    return jobs, ignored

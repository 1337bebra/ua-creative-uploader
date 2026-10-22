# UA Creative Uploader

[![Offline tests](https://github.com/1337bebra/ua-creative-uploader/actions/workflows/tests.yml/badge.svg)](https://github.com/1337bebra/ua-creative-uploader/actions/workflows/tests.yml)

Batch-upload video and static creatives to Meta ad-account media libraries from local files or Google Drive.

Built around a practical UA operations task: discover creative assets from a folder or manifest, route each asset to the right account, and retain a verifiable result without blindly repeating uncertain uploads.

**Status:** implemented with 61 passing local offline tests. Tests use fake Meta/Drive responses and block socket connections. Live account authorization, API acceptance and media processing have not yet been validated. This tool uploads library assets; it does not create ads, launch campaigns or change budgets.

## Features

- MP4 video and static JPG/JPEG/PNG, including mixed batches.
- Named product/account profiles with tokens in ignored local config or environment variables.
- Recursive folder batches: no per-file manifest entries needed for one account.
- Offline planning and read-only preflight before upload.
- Streamed multipart uploads with bounded memory use.
- Account-scoped content deduplication backed by SQLite.
- Durable intent before writes, explicit unknown outcomes and manual reconciliation.
- Per-job JSON results containing `video_id` or `image_hash`.
- Optional Google Drive source with revision and checksum checks.

## Try it without an ad account

Requires Python 3.11+. No third-party dependencies are needed for local files or tests.

```sh
python run_tests.py --report TEST_RESULTS.md
python meta_upload.py --help
```

The test suite generates synthetic media fixtures, uses fake API responses and disables socket connections. It needs no tokens and makes no advertising changes. Each CI run also publishes its generated Markdown report as an Actions artifact.

- [Generated test results](TEST_RESULTS.md): individual outcomes and source checksums.
- [Validation scope](VALIDATION.md): what the tests establish and what remains unverified.
- [GitHub Actions](https://github.com/1337bebra/ua-creative-uploader/actions): reproducible test runs on Windows and Linux.

## Use with your accounts

1. Copy `config.example.json` to `config.json`, fill in your account IDs, and remove unused example profiles.
2. Put each token in the empty `token` field of the private `config.json`. That file is Git-ignored and excluded from release packages; it contains plaintext secrets, so keep it private. Use `token_env` instead if you prefer environment variables.
3. Put your MP4/JPG/PNG assets in `creatives/`, including subfolders if useful.
4. Inspect the plan, check access, then explicitly run the upload:

```sh
python meta_upload.py --config config.json plan --directory creatives --profile product_a
python meta_upload.py --config config.json check --directory creatives --profile product_a
python meta_upload.py --config config.json upload --directory creatives --profile product_a
```

`upload` performs real external writes. Start with one small asset of each needed format. Files are processed sequentially; folder discovery removes manifest editing, not API transfer time. The full [user guide in Russian](USER_GUIDE.md) covers credentials, Drive setup, outputs and recovery commands.

Use `--jobs jobs.json` instead of `--directory ... --profile ...` when a batch needs per-file account routing, custom titles or Google Drive sources.

Minimal image job:

```json
{
  "job_id": "image_001",
  "profile": "product_a",
  "title": "banner_001",
  "source": {"type": "local", "path": "creatives/banner.png"}
}
```

Place optional jobs in a JSON array. See [mixed-batch example](jobs.mixed.example.json). Google Drive support additionally requires `python -m pip install -r requirements-drive.txt`.

## AI development instructions

The shared [AGENTS.md](AGENTS.md) defines the assistant's execution boundaries, credential handling, source inspection, verification and reporting rules. [CLAUDE.md](CLAUDE.md) points to that same instruction file.

AI assisted in breaking the workflow into modules, identifying failure paths, implementing the adapters and recovery logic, and creating executable tests. Calculations, state transitions and API calls are ordinary Python code; the uploader does not call an LLM at runtime. No production usage or measured buying impact is claimed by this repository.

## Limits and recovery

- Local deduplication only knows this state directory; pre-existing library assets require explicit adoption.
- An ambiguous POST is not automatically retried. Inspect and reconcile it before another upload.
- Image `READY` means the hash was found as ACTIVE in the selected account, not that an ad has been approved.
- JPEG/MP4 checks inspect markers/headers; PNG checks framing and CRC. These are not full media decoders. Animated PNG and formats other than MP4/JPEG/PNG are unsupported.
- A single local state directory is protected by an OS lock. Distributed workers and network filesystems are unsupported.

See [architecture](ARCHITECTURE.md) and [recovery instructions](USER_GUIDE.md#8-повторный-запуск-и-восстановление).

## Repository contents

The examples contain placeholders and synthetic test identifiers. Real account configuration, credentials, runtime state and client creatives are excluded. Keep these private when using or adapting the project.

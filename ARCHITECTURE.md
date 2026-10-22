# Architecture and operational limits

## Modules

| Module | Responsibility |
|---|---|
| meta_upload.py | CLI, preflight, batch orchestration and recovery commands |
| meta_secrets.py | Typed configuration, private token or environment reference, and folder job discovery |
| drive_fetch.py | Optional service-account authentication, streamed Drive download and revision/checksum verification |
| creative_archive.py | SQLite asset registry, transactional audit and OS process lock |
| meta_api.py | Direct Meta video/image adapter, streamed multipart, account checks and media availability |
| common.py | Media inspection, validated input, atomic JSON, bounded HTTPS reads and sanitized errors |
| run_tests.py | Network-disabled test runner and generated Markdown evidence with source fingerprints |

Each profile specifies exactly one token source: a direct value in the ignored local `config.json` or an environment-variable name. Direct values are plaintext on disk and excluded from Git by `.gitignore` and from the release allowlist. The program never prints them in its own diagnostics or reports. Environment references are bounded in length; a token mistakenly pasted into `token_env` is rejected without echoing it.

`--directory` recursively discovers supported extensions under one folder and generates jobs for one explicitly selected profile. The title is the filename stem; the job ID is a stable hash of the relative path. Unsupported files are counted and ignored. For mixed account routing, custom titles and Drive sources, `--jobs` keeps explicit per-file control. Discovery changes operator effort, not the sequential upload or Meta throughput.

## Identity and persistence

The unique asset key is `(account_id, SHA-256 of staged file bytes)`. Profile/product labels
do not override this key. A title is display metadata. A byte-identical reupload with a
different title reuses the existing mapping and does not rename the remote object.

Format is detected from content and checked against the local extension or Drive MIME.
The registry stores media_type and either video_id or image_hash. A Meta image hash is
an opaque remote identifier, not the local SHA-256. Existing registry rows without
media_type remain compatible and are interpreted as video; no SQLite schema migration
is needed because these fields live in the JSON payload.

Each source gets a generated temporary directory, independent of user filenames. Local
sources are copied to a private staged snapshot before hashing/uploading. Drive sources
are validated against size/checksum/version. A changing local file during its snapshot
is not a supported workflow; keep source files stable while running.

The durable state precedes the corresponding remote mutation:

```text
STARTING → TRANSFERRING → FINISHING → PROCESSING → READY
                   ↘ UNKNOWN_OUTCOME       ↘ PROCESSING_FAILED

Image: STARTING → UPLOADED (image_hash saved) → READY (account status ACTIVE)
           ↘ UNKNOWN_OUTCOME                    ↘ NEEDS_RECONCILIATION
```

Stored state can remain at an intent stage after a hard crash. The next matching job
converts that intent to UNKNOWN_OUTCOME, never automatically recreating the remote asset.
SQLite commits the mapping and audit event in one transaction. If local persistence
fails, execution stops; the earlier intent remains the recovery boundary. There is no
separate best-effort archive whose failure is silently ignored.

The OS lock serializes all local state commands. It has no stale timestamp to guess:
process termination releases the lock. It only coordinates a single shared local state
directory; this is not distributed exactly-once execution.

## Transport

Meta calls use fixed HTTPS hosts, explicit API version and Authorization headers. Optional
app-secret proof is derived per profile. Upload follows start/transfer/finish and observes
encoding separately, matching the protocol structure in the [official SDK](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/video_uploader.py).

Images use a single streamed multipart POST to the account's adimages edge, with filename
as the file field. Exactly one returned images entry with a valid hash is required.
Read-back uses an account-scoped hashes filter and requires ACTIVE status. Request and
response contracts follow [AdImage](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/helpers/adimagemixin.py)
and [AdAccount](https://github.com/facebook/facebook-python-business-sdk/blob/main/facebook_business/adobjects/adaccount.py)
in the official SDK. Image filenames are ASCII-normalized from the job title; the local
report retains the original title. Image upload does not imply approval for ad delivery.

Multipart chunks are iterated from disk in 1 MiB blocks, with Content-Length computed
in advance. Server-provided chunk boundaries are validated. POSTs have no automatic
retry; GETs have bounded retry. Cross-host redirects are rejected. Pagination is rebuilt
using cursors on the fixed account endpoint, never by following an arbitrary next URL.

Read-only preflight checks only the media edges used by a profile in the batch and cannot
prove upload permission. MP4 header and JPEG marker checks cannot prove decoding; PNG
framing/CRC checks do not decode pixels. APNG is rejected. No claim of API live validation
is made by the local test suite.

## Recovery

Reconcile checks a known video ID or image hash. Adopt verifies account membership and readiness but
requires the operator to verify that the remote content corresponds to the local hash.
Reset is an audited operator assertion that no remote object remains. Neither command
deletes an object. Session IDs/offsets are retained for investigation; interrupted transfers
are not resumed automatically by this version.

A media object already in Meta but absent from the local registry is not recognized automatically.
This intentionally avoids treating titles as identity. Adopt such objects before uploading
their matching local files if deduplication against an older library is needed.

## Scope

MP4 and static JPG/JPEG/PNG media-library delivery is implemented. No campaign creation,
ads, spend changes, scheduling service, LLM API call or performance analysis is included. Product
profiles organize delivery; they are not analytics or attribution configurations.

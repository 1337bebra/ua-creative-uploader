# Validation

Latest local result: **61 tests passed**, with no skips, on Windows, 23 October 2026.
The generated [TEST_RESULTS.md](TEST_RESULTS.md) contains every actual test outcome,
runtime details and source hashes. Socket connections are disabled by run_tests.py.
No live advertising or Drive requests were made by this test run.

Separate from the offline suite, the operator confirmed a live static-image upload: the
creative appeared under Account images in a Meta ad account. The account identifiers
and screenshot are not published. This is evidence for that image-library path, not
for video encoding, Drive delivery or ad approval.

The test suite runs offline with synthetic bytes and fake Meta/Drive responses. Test-only
IDs and API version values are fixtures, not production credentials or support claims.

Covered behaviors:

- Empty batches and all/trailing-duplicate batches persist complete results.
- Different content with the same title uploads separately; matching content is scoped to account.
- Start/transfer/finish failures and crash intents block blind resubmission.
- Processing, processing failure and status read failure remain distinct from readiness.
- Separate profiles use separate credentials; ignored local config tokens and environment references are supported.
- Recursive folder discovery selects one explicit profile, creates stable jobs, counts ignored files and retains deduplication.
- Offline planning does not access the network or require tokens.
- Input schemas, source paths, file size/header and duplicate job IDs are checked.
- Multipart size/slice and bounded GET retries are verified; POST is not retried.
- Credentials stay out of request URLs, upload form tokens and exception messages.
- Optional app-secret proof is present in read and multipart requests.
- Incomplete media listings cannot verify ownership.
- Drive downloads must match metadata, checksum and revision.
- Audit entries persist and competing local writers are rejected.
- JPG/PNG and MP4 route through their respective API edges, including mixed batches.
- Static image results preserve image_hash; video results preserve video_id.
- Image timeouts, missing/malformed hashes, read failures and crash intents avoid blind reupload.
- Image reconciliation/adoption and account-specific availability checks are covered.
- Extension/MIME mismatches, PNG CRC/truncation/APNG and missing JPEG end markers are rejected.
- Static images from Drive and independent account deduplication are covered.

Run: `python -m unittest discover -s tests -v`.

Generate shareable evidence: `python run_tests.py --report TEST_RESULTS.md`.
This command generates results from execution, not from a manually asserted pass count.

Not verified here: live video or Drive delivery, Google auth refresh against live credentials,
real media decoding/encoding or placement approval, macOS/Linux execution,
and distributed/network-filesystem behavior. Local testing was performed on Windows.

Before first operational use: configure one profile, run plan/check, upload one small MP4
and one image, confirm READY and their media-library objects, then rerun and confirm SKIPPED.

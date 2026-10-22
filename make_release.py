"""Build a source-only ZIP from an explicit allowlist, excluding runtime data."""
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile


FILES = [
    "README.md", "USER_GUIDE.md", "ARCHITECTURE.md", "VALIDATION.md", "TEST_RESULTS.md", "AGENTS.md", "CLAUDE.md",
    "common.py", "meta_upload.py", "meta_api.py", "meta_secrets.py", "drive_fetch.py", "creative_archive.py",
    "config.example.json", "jobs.local.example.json", "jobs.drive.example.json", "jobs.mixed.example.json",
    "requirements-drive.txt", ".gitignore", "tests/test_uploader.py", "tests/test_images.py", "run_tests.py", "make_release.py",
]


def main():
    root = Path(__file__).resolve().parent
    target = root.parent / "ua_creative_uploader.zip"
    for name in FILES:
        if not (root / name).is_file():
            raise SystemExit(f"Missing release file: {name}")
    with ZipFile(target, "w", ZIP_DEFLATED) as archive:
        for name in FILES:
            archive.write(root / name, "ua_creative_uploader/" + name)
    with ZipFile(target) as archive:
        if archive.testzip() is not None or len(archive.namelist()) != len(FILES):
            raise SystemExit("ZIP verification failed")
    print(f"Created {target.name}: {len(FILES)} source/documentation files")


if __name__ == "__main__":
    main()

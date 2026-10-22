"""Durable account/content registry, audit events and process-wide state lock."""
from __future__ import annotations

import json
import os
import sqlite3
from pathlib import Path

from common import UserError, utcnow


class StateLock:
    """An OS lock is released automatically on process exit, including crashes."""
    def __init__(self, root):
        self.path = Path(root) / "uploader.lock"

    def __enter__(self):
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.file = self.path.open("a+b")
        if self.path.stat().st_size == 0:
            self.file.write(b"0")
            self.file.flush()
        self.file.seek(0)
        try:
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(self.file.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(self.file, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except OSError:
            self.file.close()
            raise UserError("Another process is using this state directory") from None
        return self

    def __exit__(self, *args):
        self.file.close()


class Archive:
    def __init__(self, root):
        root = Path(root)
        root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(root / "archive.sqlite3")
        self.db.execute("PRAGMA journal_mode=WAL")
        self.db.execute("PRAGMA synchronous=FULL")
        self.db.executescript("""
            CREATE TABLE IF NOT EXISTS assets (
                account TEXT NOT NULL, sha256 TEXT NOT NULL, payload TEXT NOT NULL,
                PRIMARY KEY (account, sha256));
            CREATE TABLE IF NOT EXISTS events (
                id INTEGER PRIMARY KEY, timestamp TEXT NOT NULL, account TEXT NOT NULL,
                sha256 TEXT NOT NULL, payload TEXT NOT NULL);
        """)

    def get(self, account, sha256):
        row = self.db.execute("SELECT payload FROM assets WHERE account=? AND sha256=?", (account, sha256)).fetchone()
        return json.loads(row[0]) if row else None

    def put(self, account, sha256, **changes):
        value = {**(self.get(account, sha256) or {}), **changes, "account": account,
                 "sha256": sha256, "updated_at": utcnow()}
        payload = json.dumps(value, ensure_ascii=False)
        with self.db:
            self.db.execute("INSERT INTO assets VALUES (?,?,?) ON CONFLICT(account,sha256) DO UPDATE SET payload=excluded.payload",
                            (account, sha256, payload))
            self.db.execute("INSERT INTO events(timestamp,account,sha256,payload) VALUES (?,?,?,?)",
                            (utcnow(), account, sha256, payload))
        return value

    def all(self):
        return [json.loads(row[0]) for row in self.db.execute("SELECT payload FROM assets ORDER BY account,sha256")]

    def close(self):
        self.db.close()

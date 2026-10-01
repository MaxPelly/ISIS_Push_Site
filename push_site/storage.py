"""SQLite storage.

Every query is small and indexed, on a local file, so they run directly on
the event loop rather than through a thread pool.
"""
import json
import os
import sqlite3
import time
from pathlib import Path
from typing import List, Optional

SCHEMA = """
CREATE TABLE IF NOT EXISTS feed (
    id TEXT PRIMARY KEY,
    received_at REAL NOT NULL,
    payload TEXT NOT NULL
);
CREATE INDEX IF NOT EXISTS feed_received_at ON feed (received_at);
"""


class Store:
    def __init__(self, path: Path):
        if str(path) != ":memory:" and not path.exists():
            # Created owner-only; SQLite gives the -wal/-shm files the same mode.
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        self.db = sqlite3.connect(path)  # each `with self.db:` block is one transaction
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add_notification(self, notification: dict, max_items: int, max_days: float,
                         now: Optional[float] = None) -> bool:
        """Store a notification and prune the feed to its limits. False if
        one with the same id was already stored (a retry or replay)."""
        now = time.time() if now is None else now
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO feed (id, received_at, payload) VALUES (?, ?, ?)",
                (notification["id"], now, json.dumps(notification)),
            ).rowcount == 1
            # Newest by rowid (insertion order), so a clock step can't reorder it.
            self.db.execute(
                "DELETE FROM feed WHERE received_at < ? OR rowid NOT IN "
                "(SELECT rowid FROM feed ORDER BY rowid DESC LIMIT ?)",
                (now - max_days * 86400, max_items),
            )
        return inserted

    def feed(self, limit: int) -> List[dict]:
        """The newest `limit` notifications, newest first."""
        rows = self.db.execute("SELECT payload FROM feed ORDER BY rowid DESC LIMIT ?", (max(limit, 0),))
        return [json.loads(row["payload"]) for row in rows]

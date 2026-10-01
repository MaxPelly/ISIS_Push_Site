"""SQLite storage.

Every query is small and indexed, on a local file, so they run directly on
the event loop rather than through a thread pool.
"""
import hashlib
import json
import os
import secrets
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
-- Invite and device tokens are random, so a plain SHA-256 of each is stored.
CREATE TABLE IF NOT EXISTS invites (
    token_hash TEXT PRIMARY KEY,
    label TEXT NOT NULL,
    uses_left INTEGER NOT NULL,
    expires_at REAL NOT NULL
);
CREATE TABLE IF NOT EXISTS devices (
    id INTEGER PRIMARY KEY,
    token_hash TEXT NOT NULL UNIQUE,
    label TEXT NOT NULL,
    created_at REAL NOT NULL,
    last_seen REAL NOT NULL
);
"""

DEVICE_IDLE_LIMIT = 365 * 86400  # a device unused for this long has to be invited again
LAST_SEEN_RESOLUTION = 3600  # last_seen is only rewritten when this much older


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


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

    def create_invite(self, label: str, uses: int, ttl: float, now: Optional[float] = None) -> str:
        """A new invite token, valid for `uses` redemptions within `ttl` seconds."""
        now = time.time() if now is None else now
        token = secrets.token_urlsafe(32)
        with self.db:
            self.db.execute("DELETE FROM invites WHERE expires_at <= ?", (now,))
            self.db.execute(
                "INSERT INTO invites (token_hash, label, uses_left, expires_at) VALUES (?, ?, ?, ?)",
                (_hash(token), label, uses, now + ttl),
            )
        return token

    def redeem_invite(self, invite: str, now: Optional[float] = None) -> Optional[str]:
        """Use up one redemption of an invite and register a new device;
        returns the device's token, or None if the invite isn't valid."""
        now = time.time() if now is None else now
        with self.db:
            row = self.db.execute(
                "UPDATE invites SET uses_left = uses_left - 1 "
                "WHERE token_hash = ? AND uses_left > 0 AND expires_at > ? RETURNING label",
                (_hash(invite), now),
            ).fetchone()
            if row is None:
                return None
            self.db.execute("DELETE FROM invites WHERE uses_left <= 0")
            token = secrets.token_urlsafe(32)
            self.db.execute(
                "INSERT INTO devices (token_hash, label, created_at, last_seen) VALUES (?, ?, ?, ?)",
                (_hash(token), row["label"], now, now),
            )
        return token

    def device_for_token(self, token: str, now: Optional[float] = None) -> Optional[int]:
        """The id of the device with this token, if it's been used within
        DEVICE_IDLE_LIMIT; keeps its last_seen (to the hour) up to date."""
        now = time.time() if now is None else now
        row = self.db.execute(
            "SELECT id, last_seen FROM devices WHERE token_hash = ? AND last_seen > ?",
            (_hash(token), now - DEVICE_IDLE_LIMIT),
        ).fetchone()
        if row is None:
            return None
        if now - row["last_seen"] >= LAST_SEEN_RESOLUTION:
            with self.db:
                self.db.execute("UPDATE devices SET last_seen = ? WHERE id = ?", (now, row["id"]))
        return row["id"]

    def device_label(self, device_id: int) -> Optional[str]:
        row = self.db.execute("SELECT label FROM devices WHERE id = ?", (device_id,)).fetchone()
        return row["label"] if row else None

    def devices(self) -> List[sqlite3.Row]:
        return self.db.execute("SELECT id, label, created_at, last_seen FROM devices ORDER BY id").fetchall()

    def delete_device(self, device_id: int) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM devices WHERE id = ?", (device_id,)).rowcount == 1

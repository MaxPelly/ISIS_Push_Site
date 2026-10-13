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
from datetime import datetime
from typing import List, Optional, Tuple

from push_site.prefs import DEFAULT_PREFS, FEED_KINDS, OTHER_KIND

SCHEMA = """
-- The monitor's latest status (see status.py); generated_at as Unix time.
CREATE TABLE IF NOT EXISTS status (
    id INTEGER PRIMARY KEY CHECK (id = 1),
    generated_at REAL NOT NULL,
    received_at REAL NOT NULL,
    payload TEXT NOT NULL
);
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
    last_seen REAL NOT NULL,
    prefs TEXT NOT NULL  -- JSON, see prefs.parse_prefs()
);
-- Every topic seen in a notification, for the preferences page.
CREATE TABLE IF NOT EXISTS topics (
    name TEXT PRIMARY KEY,
    last_seen REAL NOT NULL
);
-- One per device (browser). Deleted with the device: revoked, forgotten or idle.
CREATE TABLE IF NOT EXISTS subscriptions (
    device_id INTEGER PRIMARY KEY REFERENCES devices (id) ON DELETE CASCADE,
    endpoint TEXT NOT NULL UNIQUE,
    p256dh TEXT NOT NULL,
    auth TEXT NOT NULL
);
"""

DEVICE_IDLE_LIMIT = 365 * 86400  # a device unused for this long has to be invited again
TOPIC_RETENTION = 90 * 86400  # topics not seen for this long drop off the preferences page
LAST_SEEN_RESOLUTION = 3600  # last_seen is only rewritten when this much older
# Each feed row's kind, for pruning (see prefs.FEED_KINDS); the names are
# fixed identifiers, so they can be written into the SQL.
_KIND = ("CASE json_extract(payload, '$.topic') "
         + " ".join(f"WHEN '{topic}' THEN '{kind}'" for topic, kind in FEED_KINDS.items())
         + f" ELSE '{OTHER_KIND}' END")


def _hash(token: str) -> str:
    return hashlib.sha256(token.encode()).hexdigest()


class Store:
    def __init__(self, path: Path):
        if str(path) != ":memory:" and not path.exists():
            path.parent.mkdir(mode=0o700, parents=True, exist_ok=True)
            # Created owner-only; SQLite gives the -wal/-shm files the same mode.
            os.close(os.open(path, os.O_CREAT | os.O_WRONLY, 0o600))
        # A short busy timeout: queries run on the event loop, so a CLI command
        # holding a write lock mustn't stall every request for the default 5s.
        self.db = sqlite3.connect(path, timeout=1)  # each `with self.db:` block is one transaction
        self.db.row_factory = sqlite3.Row
        self.db.execute("PRAGMA journal_mode = WAL")
        self.db.execute("PRAGMA foreign_keys = ON")
        self.db.executescript(SCHEMA)

    def close(self) -> None:
        self.db.close()

    def add_notification(self, notification: dict, per_kind: int, max_days: float,
                         now: Optional[float] = None) -> bool:
        """Store a notification and prune the feed to the newest `per_kind`
        of each kind, none older than `max_days`. False if one with the same
        id was already stored (a retry or replay)."""
        now = time.time() if now is None else now
        with self.db:
            inserted = self.db.execute(
                "INSERT OR IGNORE INTO feed (id, received_at, payload) VALUES (?, ?, ?)",
                (notification["id"], now, json.dumps(notification)),
            ).rowcount == 1
            # Newest by rowid (insertion order), so a clock step can't reorder it.
            self.db.execute(
                "DELETE FROM feed WHERE received_at < ? OR rowid IN (SELECT rowid FROM ("
                f"SELECT rowid, ROW_NUMBER() OVER (PARTITION BY {_KIND} ORDER BY rowid DESC) AS n FROM feed"
                ") WHERE n > ?)",
                (now - max_days * 86400, per_kind),
            )
            if inserted and notification.get("topic"):
                self.db.execute("INSERT OR REPLACE INTO topics (name, last_seen) VALUES (?, ?)",
                                (notification["topic"], now))
                self.db.execute("DELETE FROM topics WHERE last_seen < ?", (now - TOPIC_RETENTION,))
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
            self._delete_idle_devices(now)
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
                "SELECT label FROM invites WHERE token_hash = ? AND uses_left > 0 AND expires_at > ?",
                (_hash(invite), now),
            ).fetchone()
            if row is None:
                return None
            self.db.execute("UPDATE invites SET uses_left = uses_left - 1 WHERE token_hash = ?", (_hash(invite),))
            self.db.execute("DELETE FROM invites WHERE uses_left <= 0")
            token = secrets.token_urlsafe(32)
            self.db.execute(
                "INSERT INTO devices (token_hash, label, created_at, last_seen, prefs) VALUES (?, ?, ?, ?, ?)",
                (_hash(token), row["label"], now, now, json.dumps(DEFAULT_PREFS)),
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

    def _delete_idle_devices(self, now: float) -> None:
        self.db.execute("DELETE FROM devices WHERE last_seen <= ?", (now - DEVICE_IDLE_LIMIT,))

    def devices(self, now: Optional[float] = None) -> List[sqlite3.Row]:
        """The signed-in devices; ones idle too long are deleted first."""
        with self.db:
            self._delete_idle_devices(time.time() if now is None else now)
        return self.db.execute("SELECT id, label, created_at, last_seen FROM devices ORDER BY id").fetchall()

    def delete_device(self, device_id: int) -> bool:
        with self.db:
            return self.db.execute("DELETE FROM devices WHERE id = ?", (device_id,)).rowcount == 1

    def set_subscription(self, device_id: int, endpoint: str, p256dh: str, auth: str) -> None:
        """Save a device's push subscription, replacing its old one, and any
        other device's with the same endpoint (the same browser signed in again)."""
        with self.db:
            self.db.execute(
                "INSERT OR REPLACE INTO subscriptions (device_id, endpoint, p256dh, auth) VALUES (?, ?, ?, ?)",
                (device_id, endpoint, p256dh, auth),
            )

    def delete_subscription(self, device_id: int) -> None:
        with self.db:
            self.db.execute("DELETE FROM subscriptions WHERE device_id = ?", (device_id,))

    def delete_endpoint(self, endpoint: str, device_id: int) -> None:
        """Drop a subscription the push service says has gone. Only the
        device it was sent to: another may have claimed the endpoint since."""
        with self.db:
            self.db.execute("DELETE FROM subscriptions WHERE endpoint = ? AND device_id = ?", (endpoint, device_id))

    def prefs(self, device_id: int) -> dict:
        return json.loads(self.db.execute("SELECT prefs FROM devices WHERE id = ?", (device_id,)).fetchone()["prefs"])

    def set_prefs(self, device_id: int, prefs: dict) -> None:
        with self.db:
            self.db.execute("UPDATE devices SET prefs = ? WHERE id = ?", (json.dumps(prefs), device_id))

    def set_status(self, status: dict, now: Optional[float] = None) -> bool:
        """Keep the monitor's status if it's newer than the one kept; False if
        it isn't (an older one re-sent, or out of order)."""
        now = time.time() if now is None else now
        generated = datetime.fromisoformat(status["generated_at"]).timestamp()
        with self.db:
            return self.db.execute(
                "INSERT INTO status (id, generated_at, received_at, payload) VALUES (1, ?, ?, ?) "
                "ON CONFLICT (id) DO UPDATE SET generated_at = excluded.generated_at, "
                "received_at = excluded.received_at, payload = excluded.payload "
                "WHERE excluded.generated_at > status.generated_at",
                (generated, now, json.dumps(status, ensure_ascii=False)),
            ).rowcount == 1

    def status(self) -> Optional[Tuple[dict, float]]:
        """The status kept, and when it came."""
        row = self.db.execute("SELECT payload, received_at FROM status").fetchone()
        return (json.loads(row["payload"]), row["received_at"]) if row else None

    def topics(self) -> List[str]:
        return [row["name"] for row in self.db.execute("SELECT name FROM topics ORDER BY name")]

    def push_targets(self, now: Optional[float] = None) -> List[dict]:
        """Every subscription of a device that's still signed in, with its prefs."""
        now = time.time() if now is None else now
        rows = self.db.execute(
            "SELECT s.device_id, s.endpoint, s.p256dh, s.auth, d.prefs FROM subscriptions s "
            "JOIN devices d ON d.id = s.device_id WHERE d.last_seen > ?",
            (now - DEVICE_IDLE_LIMIT,),
        )
        return [{**row, "prefs": json.loads(row["prefs"])} for row in map(dict, rows)]

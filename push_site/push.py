"""Web Push: the site's VAPID key, and sending notifications to subscribed devices."""
import asyncio
import base64
import json
import logging
import os
from collections import Counter
from pathlib import Path
from typing import Optional

import aiohttp
from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02
from pywebpush import WebPushException, webpush_async

from push_site.config import ConfigError
from push_site.prefs import URGENT_SEVERITIES, wants
from push_site.storage import Store

logger = logging.getLogger(__name__)

QUEUE_SIZE = 100  # notifications waiting to be sent; the oldest is dropped when full
CONCURRENCY = 10  # pushes in flight at once
SEND_TIMEOUT = 10.0  # seconds per push request
TTL = 3600  # how long a push service keeps trying an offline device, in seconds
MAX_TITLE, MAX_TEXT = 120, 300  # characters; keeps payloads well under Web Push's 4 KB
# A subscription is dropped after this many refusals in a row (e.g. 400 or 403,
# which resending won't fix). Not at once, in case the fault is the site's;
# the app re-sends its subscription whenever it's opened, so a dropped device
# can recover.
MAX_REFUSALS = 20
# When (nearly) every device is refused with the same one of these, the fault
# is the site's, such as a bad VAPID key or clock, and that refusal isn't held
# against any device (even in a push to only one) until a push succeeds again.
# A site with one device can't tell the two apart, so still drops it after
# MAX_REFUSALS.
SITE_FAULT_STATUSES = (400, 401, 403)


def generate_vapid_key(path: Path) -> Vapid02:
    """Create a new VAPID private key file (owner-only); never overwrites one."""
    vapid = Vapid02()
    vapid.generate_keys()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ConfigError(f"{path} already exists; delete it first to replace the key "
                          "(every device will then have to subscribe again)")
    with os.fdopen(fd, "wb") as f:
        f.write(vapid.private_pem())
    return vapid


def load_vapid_key(path: Path) -> Vapid02:
    try:
        return Vapid02.from_pem(path.read_bytes())
    except OSError as exc:
        raise ConfigError(f"Could not read the VAPID key ({exc}); create one with `python -m push_site vapid`")
    except Exception as exc:  # whatever cryptography raises for a malformed PEM
        raise ConfigError(f"{path} is not a valid VAPID key: {exc}")


def public_key(vapid: Vapid02) -> str:
    """The key browsers subscribe with (applicationServerKey), base64url."""
    raw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


def push_payload(notification: dict) -> str:
    """What the service worker gets: enough to show the notification and
    open its feed entry."""
    title = f"{notification['emoji']} {notification['title']}".strip()
    body = notification["text"]
    return json.dumps({
        "id": notification["id"],
        "title": title[:MAX_TITLE],
        "body": body if len(body) <= MAX_TEXT else body[:MAX_TEXT - 1] + "…",
        "severity": notification["severity"],
    }, ensure_ascii=False)  # escaped, each emoji would take 12 bytes of the 4 KB


class Pusher:
    """Sends each new notification to the devices that want it.

    enqueue() never blocks or raises, so /ingest isn't held up; one worker
    task (run()) sends them in order, CONCURRENCY pushes at a time.
    """

    def __init__(self, store: Store, vapid: Vapid02, subject: str):
        try:  # py_vapid checks the subject when signing; fail at startup, not on every push
            vapid.sign({"sub": subject, "aud": "https://push.example"})
        except Exception as exc:
            raise ConfigError(f"[SECRETS] vapid_subject {subject!r} isn't accepted for VAPID: {exc}")
        self.store = store
        self.vapid = vapid
        self.subject = subject
        self._queue: asyncio.Queue = asyncio.Queue(QUEUE_SIZE)
        self._refusals: dict = {}  # endpoint -> consecutive refusals
        self._site_fault: Optional[int] = None  # the status of an ongoing site fault

    def enqueue(self, notification: dict) -> None:
        if self._queue.full():
            dropped = self._queue.get_nowait()
            logger.warning(f"Push queue full; dropped notification {dropped['id']}")
        self._queue.put_nowait(notification)

    async def run(self) -> None:
        async with aiohttp.ClientSession() as session:
            while True:
                notification = await self._queue.get()
                try:
                    await self.fan_out(session, notification)
                except Exception:  # never let one bad notification stop the rest
                    logger.exception(f"Pushing notification {notification['id']} failed")

    async def fan_out(self, session: aiohttp.ClientSession, notification: dict) -> None:
        subscribed = self.store.push_targets()
        live = {t["endpoint"] for t in subscribed}  # forget counts for subscriptions that have gone
        self._refusals = {e: n for e, n in self._refusals.items() if e in live}
        targets = [t for t in subscribed if self._wants(t, notification)]
        if not targets:
            return
        data = push_payload(notification)
        urgency = "high" if notification["severity"] in URGENT_SEVERITIES else "normal"
        semaphore = asyncio.Semaphore(CONCURRENCY)
        results = await asyncio.gather(
            *(self._send(session, semaphore, t, data, urgency) for t in targets), return_exceptions=True
        )
        refusals = [r for r in results if isinstance(r, int)]
        if site_fault := _site_fault_status(refusals, len(targets)):
            self._site_fault = site_fault
            logger.error(f"Pushes to {refusals.count(site_fault)} of {len(targets)} devices refused with "
                         f"HTTP {site_fault}; check the VAPID key and the server's clock")
        for target, result in zip(targets, results):
            if isinstance(result, Exception):
                logger.error(f"Push to device {target['device_id']} failed unexpectedly: {result!r}")
            elif isinstance(result, int) and result != self._site_fault:
                self._refused(target, result)

    @staticmethod
    def _wants(target: dict, notification: dict) -> bool:
        try:
            return wants(target["prefs"], notification)
        except Exception as exc:  # e.g. a timezone tzdata no longer knows; skip just this device
            logger.warning(f"Skipping device {target['device_id']}: unusable preferences ({exc!r})")
            return False

    async def _send(self, session, semaphore, target: dict, data: str, urgency: str) -> Optional[int]:
        """Push to one device: the HTTP status if the push service refused it."""
        async with semaphore:
            try:
                await webpush_async(
                    {"endpoint": target["endpoint"], "keys": {"p256dh": target["p256dh"], "auth": target["auth"]}},
                    data,
                    vapid_private_key=self.vapid,
                    vapid_claims={"sub": self.subject},  # a fresh dict: webpush_async adds this endpoint's aud
                    ttl=TTL,
                    headers={"Urgency": urgency},
                    aiohttp_session=session,
                    timeout=SEND_TIMEOUT,
                )
                self._refusals.pop(target["endpoint"], None)
                self._site_fault = None
            except WebPushException as exc:
                if exc.response is not None:
                    return exc.response.status
                logger.warning(f"Push to device {target['device_id']} failed: {type(exc).__name__}")
            except (aiohttp.ClientError, TimeoutError) as exc:
                logger.warning(f"Push to device {target['device_id']} failed: {type(exc).__name__}")
        return None

    def _refused(self, target: dict, status: int) -> None:
        endpoint = target["endpoint"]
        if status in (404, 410):  # unsubscribed
            self._remove(target, status)
            return
        # 413 (too large) and 429 (rate limited) are the site's doing, not the device's.
        if 400 <= status < 500 and status not in (413, 429):
            self._refusals[endpoint] = self._refusals.get(endpoint, 0) + 1
            if self._refusals[endpoint] >= MAX_REFUSALS:
                self._remove(target, status)
                return
        elif status >= 500:  # the push service may be having a bad day
            self._refusals.pop(endpoint, None)
        # No endpoint in the log: it's a bearer capability for that device.
        logger.warning(f"Push to device {target['device_id']} failed: HTTP {status}")

    def _remove(self, target: dict, status: int) -> None:
        self.store.delete_endpoint(target["endpoint"], target["device_id"])
        self._refusals.pop(target["endpoint"], None)
        logger.info(f"Removed device {target['device_id']}'s push subscription (HTTP {status})")


def _site_fault_status(refusals: list, attempted: int) -> Optional[int]:
    """The status that every push (or, from 5 attempts, 80% of them) was
    refused with, if it's one that points at the site rather than the devices."""
    if attempted < 2 or not refusals:
        return None
    status, count = Counter(refusals).most_common(1)[0]
    needed = attempted if attempted < 5 else 0.8 * attempted
    return status if status in SITE_FAULT_STATUSES and count >= needed else None

"""Web Push: the site's VAPID key, and sending notifications to subscribed devices."""
import asyncio
import base64
import json
import logging
import os
from pathlib import Path

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
MAX_TITLE, MAX_BODY = 120, 300  # characters; keeps payloads well under Web Push's 4 KB
# A subscription is dropped after this many refusals in a row (e.g. 400 or 403,
# which resending won't fix). Not at once: a site-wide fault, such as a bad
# VAPID setup, would otherwise wipe every device. The app re-sends its
# subscription whenever it's opened, so a dropped device can recover.
MAX_REFUSALS = 20


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
        "body": body if len(body) <= MAX_BODY else body[:MAX_BODY - 1] + "…",
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
        targets = [t for t in self.store.push_targets() if self._wants(t, notification)]
        if not targets:
            return
        data = push_payload(notification)
        urgency = "high" if notification["severity"] in URGENT_SEVERITIES else "normal"
        semaphore = asyncio.Semaphore(CONCURRENCY)
        results = await asyncio.gather(
            *(self._send(session, semaphore, t, data, urgency) for t in targets), return_exceptions=True
        )
        for target, result in zip(targets, results):
            if isinstance(result, Exception):
                logger.error(f"Push to device {target['device_id']} failed unexpectedly: {result!r}")

    @staticmethod
    def _wants(target: dict, notification: dict) -> bool:
        try:
            return wants(target["prefs"], notification)
        except Exception as exc:  # e.g. a timezone tzdata no longer knows; skip just this device
            logger.warning(f"Skipping device {target['device_id']}: unusable preferences ({exc!r})")
            return False

    async def _send(self, session, semaphore, target: dict, data: str, urgency: str) -> None:
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
            except WebPushException as exc:
                endpoint = target["endpoint"]
                status = exc.response.status if exc.response is not None else None
                if status is not None and 400 <= status < 500 and status != 429:
                    self._refusals[endpoint] = self._refusals.get(endpoint, 0) + 1
                else:  # a server error: the push service may be having a bad day
                    self._refusals.pop(endpoint, None)
                if status in (404, 410) or self._refusals.get(endpoint, 0) >= MAX_REFUSALS:  # 404/410: unsubscribed
                    self.store.delete_endpoint(endpoint)
                    self._refusals.pop(endpoint, None)
                    logger.info(f"Removed device {target['device_id']}'s push subscription (HTTP {status})")
                else:  # no endpoint in the log: it's a bearer capability for that device
                    logger.warning(f"Push to device {target['device_id']} failed: HTTP {status}")
            except (aiohttp.ClientError, asyncio.TimeoutError) as exc:
                logger.warning(f"Push to device {target['device_id']} failed: {type(exc).__name__}")

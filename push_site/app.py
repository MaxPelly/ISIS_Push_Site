"""The aiohttp application."""
import hashlib
import hmac
import json
import logging
import re
import time
from typing import Awaitable, Callable, Optional

from aiohttp import web

from push_site.config import Config
from push_site.storage import Store

logger = logging.getLogger(__name__)

# Every request this site accepts is small; MCR news text (sent as both
# text and summary) is the largest thing the monitor sends.
MAX_BODY = 64 * 1024
MAX_CLOCK_SKEW = 60  # seconds an ingest request's X-Timestamp may be off by
SEVERITIES = ("info", "good", "warning", "attention")
_ID_RE = re.compile(r"[0-9a-f-]{1,64}")  # the monitor sends uuid4s; ids also end up in URLs
MAX_TOPIC_LENGTH = 64
_TEXT_FIELDS = ("title", "text", "summary", "emoji", "flavour", "url_label", "channel", "topic")

CONFIG = web.AppKey("config", Config)
STORE = web.AppKey("store", Store)
# Called with each newly stored notification (fan-out to push subscribers).
ON_NOTIFICATION = web.AppKey("on_notification", Callable[[dict], Awaitable[None]])


def parse_notification(data: object) -> dict:
    """The fields this site uses from an ingested payload (version 1),
    type-checked; raises ValueError if it doesn't fit."""
    if not isinstance(data, dict) or data.get("v") != 1:
        raise ValueError("expected a version 1 payload")
    n = {"id": data.get("id")}
    if not isinstance(n["id"], str) or not _ID_RE.fullmatch(n["id"]):
        raise ValueError("bad id")
    for key in _TEXT_FIELDS:
        value = data.get(key, "")
        if not isinstance(value, str):
            raise ValueError(f"bad {key}")
        n[key] = value
    if len(n["topic"]) > MAX_TOPIC_LENGTH:
        raise ValueError("bad topic")
    n["severity"] = data.get("severity")
    if n["severity"] not in SEVERITIES:
        raise ValueError("bad severity")
    facts = data.get("facts", [])
    if not (isinstance(facts, list) and all(
        isinstance(f, list) and len(f) == 2 and all(isinstance(x, str) for x in f) for f in facts
    )):
        raise ValueError("bad facts")
    n["facts"] = facts
    # Only web links, so a feed entry can never hold e.g. a javascript: URL.
    url = data.get("url")
    if url is not None and not (isinstance(url, str) and url.startswith(("https://", "http://"))):
        raise ValueError("bad url")
    n["url"] = url
    timestamp = data.get("timestamp")
    if timestamp is not None and not isinstance(timestamp, str):
        raise ValueError("bad timestamp")
    n["timestamp"] = timestamp
    return n


def _signature_ok(secret: bytes, timestamp: str, signature: str, body: bytes) -> bool:
    # Plain digits only: int() would also take e.g. "+1_0" or a huge number.
    if not (timestamp.isascii() and timestamp.isdigit() and len(timestamp) <= 12):
        return False
    if abs(time.time() - int(timestamp)) > MAX_CLOCK_SKEW or not signature.isascii():
        return False
    expected = hmac.new(secret, timestamp.encode() + b"." + body, hashlib.sha256).hexdigest()
    return hmac.compare_digest(expected, signature)


async def ingest(request: web.Request) -> web.Response:
    """Take a signed notification from the monitor daemon, which calls the
    site directly on localhost. The reverse proxy should refuse this path;
    a forwarded request is refused here too, in case it doesn't."""
    if "X-Forwarded-For" in request.headers or "Forwarded" in request.headers:
        raise web.HTTPNotFound()
    config = request.app[CONFIG]
    body = await request.read()
    headers = request.headers
    if not _signature_ok(config.ingest_secret, headers.get("X-Timestamp", ""), headers.get("X-Signature", ""), body):
        logger.warning("Rejected an ingest request with a bad or stale signature")
        raise web.HTTPUnauthorized()
    try:
        notification = parse_notification(json.loads(body))
    except (ValueError, RecursionError) as exc:  # including JSONDecodeError and UnicodeDecodeError
        logger.warning(f"Rejected an ingest payload: {exc}")
        raise web.HTTPBadRequest(text=str(exc))
    if request.app[STORE].add_notification(notification, config.feed_max_items, config.feed_max_days):
        await request.app[ON_NOTIFICATION](notification)
    return web.Response(status=204)


async def _ignore(notification: dict) -> None:
    pass


def create_app(config: Config, store: Store,
               on_notification: Optional[Callable[[dict], Awaitable[None]]] = None) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY)
    app[CONFIG] = config
    app[STORE] = store
    app[ON_NOTIFICATION] = on_notification or _ignore
    app.router.add_post("/ingest", ingest)
    return app

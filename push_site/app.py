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
from push_site.storage import DEVICE_IDLE_LIMIT, Store

logger = logging.getLogger(__name__)

# Every request this site accepts is small; MCR news text (sent as both
# text and summary) is the largest thing the monitor sends.
MAX_BODY = 64 * 1024
MAX_CLOCK_SKEW = 60  # seconds an ingest request's X-Timestamp may be off by
SEVERITIES = ("info", "good", "warning", "attention")
_ID_RE = re.compile(r"[0-9a-f-]{1,64}")  # the monitor sends uuid4s; ids also end up in URLs
MAX_TOPIC_LENGTH = 64
_TEXT_FIELDS = ("title", "text", "summary", "emoji", "flavour", "url_label", "channel", "topic")

# __Host- makes browsers insist on Secure, Path=/ and no Domain.
COOKIE = "__Host-device"
SECURITY_HEADERS = {
    "Content-Security-Policy": (
        "default-src 'self'; img-src 'self' data:; object-src 'none'; base-uri 'none'; "
        "form-action 'self'; frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "Referrer-Policy": "no-referrer",
}

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


DEVICE = "device_id"  # request key: the signed-in device, set by api_guard


@web.middleware
async def api_guard(request: web.Request, handler):
    """Every /api/ request that changes something must come from the site's
    own pages (Origin) as JSON; all but /api/redeem need a device cookie."""
    if request.path.startswith("/api/"):
        if request.method not in ("GET", "HEAD") and (
            request.headers.get("Origin") != request.app[CONFIG].public_origin
            or request.content_type != "application/json"
        ):
            raise web.HTTPForbidden()
        if request.path != "/api/redeem":
            token = request.cookies.get(COOKIE)
            device_id = request.app[STORE].device_for_token(token) if token else None
            if device_id is None:
                raise web.HTTPUnauthorized()
            request[DEVICE] = device_id
    return await handler(request)


async def _add_security_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.update(SECURITY_HEADERS)
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"


async def _json_object(request: web.Request) -> dict:
    try:
        data = await request.json()
    except ValueError:
        data = None
    if not isinstance(data, dict):
        raise web.HTTPBadRequest(text="expected a JSON object")
    return data


def _set_device_cookie(response: web.Response, token: str) -> None:
    response.set_cookie(COOKIE, token, max_age=DEVICE_IDLE_LIMIT, path="/",
                        secure=True, httponly=True, samesite="Strict")


async def redeem(request: web.Request) -> web.Response:
    """Swap an invite (from the #invite= part of an invite link, which never
    reaches the server's logs) for a device cookie."""
    invite = (await _json_object(request)).get("invite")
    token = request.app[STORE].redeem_invite(invite) if isinstance(invite, str) and invite else None
    if token is None:
        raise web.HTTPForbidden(text="This invite is invalid, used up or expired.")
    response = web.json_response({"ok": True})
    _set_device_cookie(response, token)
    return response


async def me(request: web.Request) -> web.Response:
    response = web.json_response({"label": request.app[STORE].device_label(request[DEVICE])})
    # Renewed whenever the app opens, so only a year of disuse signs a device out.
    _set_device_cookie(response, request.cookies[COOKIE])
    return response


async def forget_me(request: web.Request) -> web.Response:
    request.app[STORE].delete_device(request[DEVICE])
    response = web.Response(status=204)
    response.del_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="Strict")
    return response


async def _ignore(notification: dict) -> None:
    pass


def create_app(config: Config, store: Store,
               on_notification: Optional[Callable[[dict], Awaitable[None]]] = None) -> web.Application:
    app = web.Application(client_max_size=MAX_BODY, middlewares=[api_guard])
    app[CONFIG] = config
    app[STORE] = store
    app[ON_NOTIFICATION] = on_notification or _ignore
    app.on_response_prepare.append(_add_security_headers)
    app.router.add_post("/ingest", ingest)
    app.router.add_post("/api/redeem", redeem)
    app.router.add_get("/api/me", me)
    app.router.add_delete("/api/me", forget_me)
    return app

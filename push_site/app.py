"""The aiohttp application."""
import base64
import binascii
import hashlib
import hmac
import json
import logging
import re
import time
from typing import Callable, Optional
from urllib.parse import urlsplit

from aiohttp import web
from cryptography.hazmat.primitives.asymmetric import ec
from aiohttp.web_protocol import RequestPayloadError

from push_site.config import Config
from push_site.prefs import BASE_TOPICS, parse_prefs
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
    # Not no-referrer: browsers then send "Origin: null" on the site's own
    # POSTs, failing same_origin_guard. Invite links keep their token in
    # the #fragment, which is never sent as a referrer.
    "Referrer-Policy": "same-origin",
}

TOKEN_RE = re.compile(r"[A-Za-z0-9_-]{43}")  # secrets.token_urlsafe(32), as invites and devices use

# Hosts of the browsers' push services; subscriptions elsewhere are refused,
# so the site can't be made to send requests to arbitrary servers.
PUSH_SERVICE_HOSTS = ("fcm.googleapis.com", ".push.apple.com", ".push.services.mozilla.com", ".notify.windows.com")
_KEY_RE = re.compile(r"[A-Za-z0-9_-]+={0,2}")
_HOST_RE = re.compile(r"[a-z0-9.-]+")

CONFIG = web.AppKey("config", Config)
VAPID_PUBLIC_KEY = web.AppKey("vapid_public_key", str)
STORE = web.AppKey("store", Store)
# Called with each newly stored notification (fan-out to push subscribers);
# it must not block or raise, since the notification is already stored.
ON_NOTIFICATION = web.AppKey("on_notification", Callable[[dict], None])


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
    body = await _read_body(request)
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
        request.app[ON_NOTIFICATION](notification)
    return web.Response(status=204)


@web.middleware
async def same_origin_guard(request: web.Request, handler):
    """Every /api/ request that changes something must come from the site's
    own pages (Origin) as JSON, which a cross-site form can't send."""
    if request.path.startswith("/api/") and request.method not in ("GET", "HEAD") and (
        request.headers.get("Origin") != request.app[CONFIG].public_origin
        or request.content_type != "application/json"
    ):
        raise web.HTTPForbidden()
    return await handler(request)


def signed_in(handler):
    """For handlers that need a device cookie: called as handler(request, device_id)."""
    async def check_cookie(request: web.Request) -> web.StreamResponse:
        token = request.cookies.get(COOKIE, "")
        device_id = request.app[STORE].device_for_token(token) if TOKEN_RE.fullmatch(token) else None
        if device_id is None:
            raise web.HTTPUnauthorized()
        return await handler(request, device_id)
    return check_cookie


async def _add_security_headers(request: web.Request, response: web.StreamResponse) -> None:
    response.headers.update(SECURITY_HEADERS)
    if request.path.startswith("/api/"):
        response.headers["Cache-Control"] = "no-store"


async def _read_body(request: web.Request) -> bytes:
    try:
        return await request.read()
    except RequestPayloadError:  # e.g. a body that doesn't match its Content-Encoding
        raise web.HTTPBadRequest()


async def _json_object(request: web.Request) -> dict:
    try:
        data = json.loads(await _read_body(request))
    except (ValueError, RecursionError):
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
    token = request.app[STORE].redeem_invite(invite) if isinstance(invite, str) and TOKEN_RE.fullmatch(invite) else None
    if token is None:
        raise web.HTTPForbidden(text="This invite is invalid, used up or expired.")
    response = web.json_response({"ok": True})
    _set_device_cookie(response, token)
    return response


@signed_in
async def me(request: web.Request, device_id: int) -> web.Response:
    response = web.json_response({"label": request.app[STORE].device_label(device_id)})
    # Renewed whenever the app opens, so only a year of disuse signs a device out.
    _set_device_cookie(response, request.cookies[COOKIE])
    return response


@signed_in
async def forget_me(request: web.Request, device_id: int) -> web.Response:
    request.app[STORE].delete_device(device_id)
    response = web.Response(status=204)
    response.del_cookie(COOKIE, path="/", secure=True, httponly=True, samesite="Strict")
    return response


@signed_in
async def vapid_key(request: web.Request, device_id: int) -> web.Response:
    return web.json_response({"key": request.app[VAPID_PUBLIC_KEY]})


def push_endpoint_ok(endpoint: object) -> bool:
    if not isinstance(endpoint, str) or len(endpoint) > 1024:
        return False
    try:
        url = urlsplit(endpoint)
        port = url.port
    except ValueError:
        return False
    host = url.hostname or ""
    return (url.scheme == "https" and bool(_HOST_RE.fullmatch(host)) and "@" not in url.netloc and port in (None, 443)
            and any(host == h or (h.startswith(".") and host.endswith(h)) for h in PUSH_SERVICE_HOSTS))


def _decode_key(value: object) -> Optional[bytes]:
    if not (isinstance(value, str) and len(value) <= 100 and _KEY_RE.fullmatch(value)):
        return None
    try:
        return base64.urlsafe_b64decode(value.rstrip("=") + "=" * (-len(value.rstrip("=")) % 4))
    except (binascii.Error, ValueError):
        return None


def keys_ok(p256dh: object, auth: object) -> bool:
    """Whether a subscription's keys can be encrypted to: a P-256 point and a 16-byte secret."""
    point, secret = _decode_key(p256dh), _decode_key(auth)
    if point is None or secret is None or len(secret) != 16:
        return False
    try:
        ec.EllipticCurvePublicKey.from_encoded_point(ec.SECP256R1(), point)
    except ValueError:
        return False
    return True


@signed_in
async def put_subscription(request: web.Request, device_id: int) -> web.Response:
    """Save the browser's PushSubscription (as from its toJSON())."""
    data = await _json_object(request)
    keys = data.get("keys") if isinstance(data.get("keys"), dict) else {}
    if not (push_endpoint_ok(data.get("endpoint")) and keys_ok(keys.get("p256dh"), keys.get("auth"))):
        raise web.HTTPBadRequest(text="not a push subscription from a supported browser")
    request.app[STORE].set_subscription(device_id, data["endpoint"], keys["p256dh"], keys["auth"])
    return web.Response(status=204)


@signed_in
async def delete_subscription(request: web.Request, device_id: int) -> web.Response:
    request.app[STORE].delete_subscription(device_id)
    return web.Response(status=204)


@signed_in
async def get_prefs(request: web.Request, device_id: int) -> web.Response:
    return web.json_response(request.app[STORE].prefs(device_id))


@signed_in
async def put_prefs(request: web.Request, device_id: int) -> web.Response:
    try:
        prefs = parse_prefs(await _json_object(request))
    except ValueError as exc:
        raise web.HTTPBadRequest(text=str(exc))
    request.app[STORE].set_prefs(device_id, prefs)
    return web.Response(status=204)


@signed_in
async def topics(request: web.Request, device_id: int) -> web.Response:
    """The topics to offer: the fixed ones, then instruments seen recently."""
    seen = [t for t in request.app[STORE].topics() if t not in BASE_TOPICS]
    return web.json_response(list(BASE_TOPICS) + seen)


MAX_FEED = 200


@signed_in
async def feed(request: web.Request, device_id: int) -> web.Response:
    try:
        limit = min(int(request.query.get("limit", "50")), MAX_FEED)
    except ValueError:
        raise web.HTTPBadRequest(text="limit must be a number")
    return web.json_response(request.app[STORE].feed(limit))


def _ignore(notification: dict) -> None:
    pass


def create_app(config: Config, store: Store,
               on_notification: Optional[Callable[[dict], None]] = None,
               vapid_public_key: str = "") -> web.Application:
    app = web.Application(client_max_size=MAX_BODY, middlewares=[same_origin_guard])
    app[CONFIG] = config
    app[STORE] = store
    app[ON_NOTIFICATION] = on_notification or _ignore
    app[VAPID_PUBLIC_KEY] = vapid_public_key
    app.on_response_prepare.append(_add_security_headers)
    app.router.add_post("/ingest", ingest)
    app.router.add_post("/api/redeem", redeem)
    app.router.add_get("/api/me", me)
    app.router.add_delete("/api/me", forget_me)
    app.router.add_get("/api/vapid-key", vapid_key)
    app.router.add_put("/api/subscription", put_subscription)
    app.router.add_delete("/api/subscription", delete_subscription)
    app.router.add_get("/api/prefs", get_prefs)
    app.router.add_put("/api/prefs", put_prefs)
    app.router.add_get("/api/topics", topics)
    app.router.add_get("/api/feed", feed)
    return app

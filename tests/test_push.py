import asyncio
import base64
import json
import os
from unittest.mock import AsyncMock, MagicMock, patch

import aiohttp
import http_ece
import pytest
from aiohttp import web
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec
from py_vapid import Vapid02
from pywebpush import WebPushException

from push_site import push
from push_site.push import Pusher, push_payload
from push_site.storage import DEVICE_IDLE_LIMIT, Store
from push_site.config import ConfigError
from tests.helpers import payload

SUBJECT = "mailto:admin@example.org"


def b64(raw: bytes) -> str:
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()


@pytest.fixture
def vapid():
    key = Vapid02()
    key.generate_keys()
    return key


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "push.db")
    yield store
    store.close()


def add_device(store, endpoint, topics=("TS1",), quiet=None):
    token = store.redeem_invite(store.create_invite("x", uses=1, ttl=60))
    device_id = store.device_for_token(token)
    store.set_prefs(device_id, {"topics": list(topics), "quiet": quiet})
    store.set_subscription(device_id, endpoint, "B" + "x" * 86, "a" * 22)
    return device_id


def notification(**overrides):
    return {k: v for k, v in payload(**overrides).items() if k != "v"}


def test_push_payload_is_small_and_truncated():
    data = json.loads(push_payload(notification(text="x" * 1000, title="t" * 200)))
    assert data["id"] == "0f0e-1" and data["severity"] == "attention"
    assert len(data["title"]) == 120 and data["title"].startswith("🔴 ")
    assert len(data["body"]) == 300 and data["body"].endswith("…")
    assert json.loads(push_payload(notification(text="short")))["body"] == "short"


async def test_fan_out_sends_only_to_matching_devices(store, vapid):
    add_device(store, "https://fcm.googleapis.com/a", topics=["TS1"])
    add_device(store, "https://fcm.googleapis.com/b", topics=["TS2"])
    pusher = Pusher(store, vapid, SUBJECT)
    with patch.object(push, "webpush_async", new=AsyncMock()) as send:
        await pusher.fan_out(MagicMock(), notification())
    send.assert_awaited_once()
    args, kwargs = send.await_args
    assert args[0]["endpoint"] == "https://fcm.googleapis.com/a"
    assert kwargs["headers"] == {"Urgency": "high"}  # attention
    assert kwargs["vapid_claims"] == {"sub": SUBJECT}
    assert kwargs["timeout"] == push.SEND_TIMEOUT and kwargs["ttl"] == push.TTL


async def test_each_push_gets_its_own_claims(store, vapid):
    for name in "ab":
        add_device(store, f"https://fcm.googleapis.com/{name}")
    pusher = Pusher(store, vapid, SUBJECT)
    with patch.object(push, "webpush_async", new=AsyncMock()) as send:
        await pusher.fan_out(MagicMock(), notification(severity="info"))
    claims = [c.kwargs["vapid_claims"] for c in send.await_args_list]
    assert len(claims) == 2 and claims[0] is not claims[1]
    assert send.await_args.kwargs["headers"] == {"Urgency": "normal"}


async def test_signed_out_devices_get_nothing(store, vapid):
    add_device(store, "https://fcm.googleapis.com/a")
    store.db.execute("UPDATE devices SET last_seen = last_seen - ?", (DEVICE_IDLE_LIMIT,))
    store.db.commit()
    with patch.object(push, "webpush_async", new=AsyncMock()) as send:
        await Pusher(store, vapid, SUBJECT).fan_out(MagicMock(), notification())
    send.assert_not_awaited()


@pytest.mark.parametrize("status, kept", [(410, False), (404, False), (500, True), (413, True)])
async def test_gone_subscriptions_are_removed(store, vapid, caplog, status, kept):
    add_device(store, "https://fcm.googleapis.com/secret-endpoint")
    error = WebPushException("failed", response=MagicMock(status=status))
    with patch.object(push, "webpush_async", new=AsyncMock(side_effect=error)):
        await Pusher(store, vapid, SUBJECT).fan_out(MagicMock(), notification())
    assert bool(store.push_targets()) is kept
    assert "secret-endpoint" not in caplog.text


async def test_network_errors_are_logged_and_other_devices_still_get_theirs(store, vapid, caplog):
    for name in "ab":
        add_device(store, f"https://fcm.googleapis.com/{name}")
    send = AsyncMock(side_effect=[aiohttp.ClientConnectionError("refused"), None])
    with patch.object(push, "webpush_async", new=send):
        await Pusher(store, vapid, SUBJECT).fan_out(MagicMock(), notification())
    assert send.await_count == 2
    assert "failed: ClientConnectionError" in caplog.text


def test_subject_is_checked_up_front(store, vapid):
    with pytest.raises(ConfigError, match="vapid_subject"):
        Pusher(store, vapid, "mailto:nobody")


async def test_full_queue_drops_the_oldest(store, vapid, caplog):
    with patch.object(push, "QUEUE_SIZE", 2):
        pusher = Pusher(store, vapid, SUBJECT)
    for i in range(3):
        pusher.enqueue(notification(id=str(i)))
    assert [pusher._queue.get_nowait()["id"] for _ in range(2)] == ["1", "2"]
    assert "dropped notification 0" in caplog.text


async def test_worker_survives_a_failing_notification(store, vapid, caplog):
    pusher = Pusher(store, vapid, SUBJECT)
    done = asyncio.Event()
    calls = []

    async def fan_out(session, n):
        calls.append(n["id"])
        if n["id"] == "1":
            raise RuntimeError("boom")
        done.set()
    pusher.fan_out = fan_out
    pusher.enqueue(notification(id="1"))
    pusher.enqueue(notification(id="2"))
    task = asyncio.create_task(pusher.run())
    await asyncio.wait_for(done.wait(), 2)
    task.cancel()
    assert calls == ["1", "2"]
    assert "Pushing notification 1 failed" in caplog.text


async def test_real_push_is_encrypted_for_the_browser(store, vapid, aiohttp_server):
    """End to end against a fake push service: VAPID-signed, aes128gcm
    encrypted to the subscription's keys, and decryptable by the browser."""
    browser_key = ec.generate_private_key(ec.SECP256R1())
    p256dh = b64(browser_key.public_key().public_bytes(
        serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint))
    auth = os.urandom(16)
    requests = []

    async def push_service(request):
        requests.append((request.headers.copy(), await request.read()))  # case-insensitive
        return web.Response(status=201)
    app = web.Application()
    app.router.add_post("/push/abc", push_service)
    server = await aiohttp_server(app)

    token = store.redeem_invite(store.create_invite("x", uses=1, ttl=60))
    store.set_subscription(store.device_for_token(token), str(server.make_url("/push/abc")), p256dh, b64(auth))
    async with aiohttp.ClientSession() as session:
        await Pusher(store, vapid, SUBJECT).fan_out(session, notification())

    (headers, body), = requests
    assert headers["Content-Encoding"] == "aes128gcm"
    assert headers["Authorization"].startswith("vapid t=")
    assert (headers["TTL"], headers["Urgency"]) == (str(push.TTL), "high")
    plain = http_ece.decrypt(body, private_key=browser_key, auth_secret=auth, version="aes128gcm")
    assert json.loads(plain)["title"] == "🔴 TS1 ⬇️ high → off"


async def test_ingested_notifications_are_pushed(tmp_path, aiohttp_client, vapid):
    from push_site.__main__ import build_app
    from tests.helpers import make_config, signed
    (tmp_path / "vapid.pem").write_bytes(vapid.private_pem())
    config = make_config(tmp_path)
    store = Store(tmp_path / "push.db")
    add_device(store, "https://fcm.googleapis.com/a")
    sent = asyncio.Event()
    with patch.object(push, "webpush_async", new=AsyncMock(side_effect=lambda *a, **k: sent.set())):
        client = await aiohttp_client(build_app(config, store))
        assert (await client.post("/ingest", **signed(payload()))).status == 204
        await asyncio.wait_for(sent.wait(), 2)
        await client.close()
    store.close()

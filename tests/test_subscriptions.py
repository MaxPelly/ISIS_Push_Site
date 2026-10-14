import pytest
from cryptography.hazmat.primitives import serialization
from cryptography.hazmat.primitives.asymmetric import ec

from push_site.app import keys_ok, push_endpoint_ok
from tests.helpers import api, b64, signed_in


POINT = ec.generate_private_key(ec.SECP256R1()).public_key().public_bytes(
    serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
P256DH, AUTH = b64(POINT), b64(bytes(16))


def subscription(endpoint="https://fcm.googleapis.com/fcm/send/abc", p256dh=P256DH, auth=AUTH):
    return {"endpoint": endpoint, "expirationTime": None, "keys": {"p256dh": p256dh, "auth": auth}}


def stored(site):
    return [tuple(row) for row in site.store.db.execute("SELECT device_id, endpoint FROM subscriptions")]


async def test_vapid_key(site):
    resp = await api(site, "GET", "/api/vapid-key", token=await signed_in(site))
    assert await resp.json() == {"key": "BPublicKey"}


async def test_subscribe_replace_and_unsubscribe(site):
    token = await signed_in(site)
    assert (await api(site, "PUT", "/api/subscription", subscription(), token=token)).status == 204
    new = "https://web.push.apple.com/QGuQyavXutnMbXv"
    assert (await api(site, "PUT", "/api/subscription", subscription(new), token=token)).status == 204
    assert stored(site) == [(1, new)]
    assert (await api(site, "DELETE", "/api/subscription", {}, token=token)).status == 204
    assert stored(site) == []


async def test_same_endpoint_moves_to_the_newest_device(site):
    first, second = await signed_in(site), await signed_in(site)
    await api(site, "PUT", "/api/subscription", subscription(), token=first)
    await api(site, "PUT", "/api/subscription", subscription(), token=second)
    assert stored(site) == [(2, "https://fcm.googleapis.com/fcm/send/abc")]


async def test_subscription_goes_with_its_device(site):
    token = await signed_in(site)
    await api(site, "PUT", "/api/subscription", subscription(), token=token)
    await api(site, "DELETE", "/api/me", {}, token=token)
    assert stored(site) == []


@pytest.mark.parametrize("endpoint, ok", [
    ("https://fcm.googleapis.com/fcm/send/abc", True),
    ("https://web.push.apple.com/abc", True),
    ("https://updates.push.services.mozilla.com/wpush/v2/abc", True),
    ("https://wns2-par02p.notify.windows.com/w/?token=abc", True),
    ("https://fcm.googleapis.com:443/fcm/send/abc", True),
    ("http://fcm.googleapis.com/fcm/send/abc", False),
    ("https://fcm.googleapis.com:8443/x", False),
    ("https://evilfcm.googleapis.com.example/x", False),
    ("https://push.apple.com.evil.example/x", False),
    ("https://evil.example/?h=.push.apple.com", False),
    ("https://user@fcm.googleapis.com/x", False),
    ("https://127.0.0.1/x", False),
    ("https://[::1/x", False),
    ("https://evil.com%2f.push.apple.com/x", False),
    ("https://evil.com;.push.apple.com/x", False),
    ("https://fcm.googleapis.com/" + "x" * 1024, False),
    (5, False),
])
def test_only_push_service_endpoints_are_allowed(endpoint, ok):
    assert push_endpoint_ok(endpoint) is ok


@pytest.mark.parametrize("body", [
    subscription("https://evil.example/x"),
    subscription(p256dh="short"),
    subscription(p256dh="B" + "<" * 86),
    subscription(p256dh="B" + "x" * 86),  # right length, not a point on the curve
    subscription(auth=""),
    subscription(auth=b64(bytes(15))),
    {"endpoint": "https://fcm.googleapis.com/fcm/send/abc"},
    {"endpoint": "https://fcm.googleapis.com/fcm/send/abc", "keys": []},
])
async def test_bad_subscriptions_are_refused(site, body):
    token = await signed_in(site)
    assert (await api(site, "PUT", "/api/subscription", body, token=token)).status == 400
    assert stored(site) == []


def test_keys_must_be_usable():
    assert keys_ok(P256DH, AUTH)
    assert keys_ok(P256DH + "=", AUTH + "==")  # padding allowed
    assert not keys_ok(P256DH[:-1], AUTH)
    assert not keys_ok(b64(b"\x04" + bytes(64)), AUTH)
    assert not keys_ok(P256DH, None)

import time

import pytest

from push_site.app import create_app
from push_site.storage import Store
from tests.helpers import make_config, payload, signed


@pytest.fixture
async def site(tmp_path, aiohttp_client):
    store = Store(tmp_path / "push.db")
    received = []

    async def on_notification(n):
        received.append(n)
    client = await aiohttp_client(create_app(make_config(tmp_path), store, on_notification))
    client.received, client.store = received, store
    yield client
    store.close()


async def test_signed_notification_is_stored_and_passed_on_once(site):
    resp = await site.post("/ingest", **signed(payload()))
    assert resp.status == 204
    assert site.store.feed(10)[0]["title"] == "TS1 ⬇️ high → off"
    assert [n["id"] for n in site.received] == ["id-1"]
    # A retry or replay of the same id is accepted but not passed on again.
    assert (await site.post("/ingest", **signed(payload()))).status == 204
    assert len(site.received) == 1


@pytest.mark.parametrize("kwargs", [
    {"secret": b"x" * 32},
    {"timestamp": int(time.time()) - 120},
    {"timestamp": int(time.time()) + 120},
    {"timestamp": "soon"},
])
async def test_bad_or_stale_signature_is_rejected(site, kwargs):
    resp = await site.post("/ingest", **signed(payload(), **kwargs))
    assert resp.status == 401
    assert site.store.feed(10) == [] and site.received == []


async def test_unsigned_or_tampered_requests_are_rejected(site):
    assert (await site.post("/ingest", json=payload())).status == 401
    request = signed(payload())
    request["data"] = request["data"].replace(b"TS1", b"TS2")
    assert (await site.post("/ingest", **request)).status == 401
    request = signed(payload())
    request["headers"]["X-Signature"] = "é" * 64
    assert (await site.post("/ingest", **request)).status == 401


async def test_forwarded_requests_are_refused(site):
    request = signed(payload())
    request["headers"]["X-Forwarded-For"] = "203.0.113.9"
    assert (await site.post("/ingest", **request)).status == 404


async def test_oversized_body_is_refused(site):
    assert (await site.post("/ingest", **signed(payload(text="x" * 20_000)))).status == 413


@pytest.mark.parametrize("data", [
    b"not json",
    b"\xff\xfe",
    payload(v=2),
    payload(id=""),
    payload(id="x" * 65),
    payload(title=None),
    payload(severity="panic"),
    payload(facts=[["only one"]]),
    payload(url="javascript:alert(1)"),
    payload(timestamp=5),
    [1, 2],
])
async def test_malformed_payloads_are_rejected(site, data):
    assert (await site.post("/ingest", **signed(data))).status == 400
    assert site.store.feed(10) == []


async def test_unknown_fields_are_dropped(site):
    await site.post("/ingest", **signed(payload(extra="ignored", url="https://example.org/mcr")))
    stored = site.store.feed(1)[0]
    assert "extra" not in stored and "v" not in stored
    assert stored["url"] == "https://example.org/mcr"

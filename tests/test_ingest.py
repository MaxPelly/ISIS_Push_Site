import time

import pytest
from aiohttp import web

from tests.helpers import payload, signed


async def test_signed_notification_is_stored_and_passed_on_once(site):
    resp = await site.post("/ingest", **signed(payload()))
    assert resp.status == 204
    assert site.store.feed(10)[0]["title"] == "TS1 ⬇️ high → off"
    assert [n["id"] for n in site.received] == ["0f0e-1"]
    # A retry or replay of the same id is accepted but not passed on again.
    assert (await site.post("/ingest", **signed(payload()))).status == 204
    assert len(site.received) == 1


@pytest.mark.parametrize("kwargs", [
    {"secret": b"x" * 32},
    {"timestamp": int(time.time()) - 120},
    {"timestamp": int(time.time()) + 120},
    {"timestamp": "soon"},
    {"timestamp": "9" * 400},
    {"timestamp": "+" + str(int(time.time()))},
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


@pytest.mark.parametrize("header", ["X-Forwarded-For", "Forwarded", "CF-Connecting-IP"])
async def test_forwarded_requests_are_refused(site, header):
    request = signed(payload())
    request["headers"][header] = "for=203.0.113.9"
    assert (await site.post("/ingest", **request)).status == 404


@pytest.mark.parametrize("remote", ["172.18.0.5", "203.0.113.9", "::ffff:10.0.0.6", None])
async def test_requests_from_other_hosts_are_refused(site, monkeypatch, remote):
    """E.g. from a container on a Docker network, when the site listens on 0.0.0.0."""
    monkeypatch.setattr(web.Request, "remote", property(lambda self: remote))
    assert (await site.post("/ingest", **signed(payload()))).status == 404
    assert site.received == []


async def test_oversized_body_is_refused(site):
    assert (await site.post("/ingest", **signed(payload(text="é" * 20_000)))).status == 204  # 40 KB
    assert (await site.post("/ingest", **signed(payload(id="2", text="x" * 70_000)))).status == 413


@pytest.mark.parametrize("data", [
    b"not json",
    b"\xff\xfe",
    payload(v=2),
    payload(id=""),
    payload(id="a" * 65),
    payload(id="<img src=x onerror=alert(1)>"),
    payload(topic="T" * 65),
    pytest.param(b"[" * 15000, id="deeply-nested"),
    payload(title=None),
    payload(severity="panic"),
    payload(facts=[["only one"]]),
    payload(url="javascript:alert(1)"),
    payload(timestamp=5),
    pytest.param(b'{"v": 1, "id": "1", "severity": "info", "text": "\\ud800"}', id="lone-surrogate"),
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

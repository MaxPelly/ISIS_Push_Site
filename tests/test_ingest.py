import sqlite3
import time
from unittest.mock import patch

import pytest
from aiohttp import web

from tests.helpers import payload, signed


async def test_signed_notification_is_stored_and_passed_on_once(site):
    resp = await site.post("/ingest", **signed(payload(extra="ignored", url="https://example.org/mcr")))
    assert resp.status == 204
    stored = site.store.feed(10)[0]
    assert stored["title"] == "TS1 ⬇️ high → off" and stored["url"] == "https://example.org/mcr"
    assert "extra" not in stored and "v" not in stored  # unknown fields are dropped
    assert [n["id"] for n in site.received] == ["0f0e-1"]
    # A retry or replay of the same id is accepted but not passed on again.
    assert (await site.post("/ingest", **signed(payload()))).status == 204
    assert len(site.received) == 1


def _tampered(body: bytes = None, **headers) -> dict:
    """A signed request, then changed."""
    request = signed(payload())
    request["data"] = body or request["data"]
    request["headers"].update(headers)
    return request


@pytest.mark.parametrize("request_kwargs", [
    pytest.param(lambda: {"json": payload()}, id="unsigned"),
    pytest.param(lambda: _tampered(signed(payload())["data"].replace(b"TS1", b"TS2")), id="tampered-body"),
    pytest.param(lambda: _tampered(**{"X-Signature": "é" * 64}), id="non-ascii-signature"),
    pytest.param(lambda: signed(payload(), secret=b"x" * 32), id="wrong-secret"),
    pytest.param(lambda: signed(payload(), timestamp=int(time.time()) - 120), id="stale"),
    pytest.param(lambda: signed(payload(), timestamp=int(time.time()) + 120), id="future"),
    pytest.param(lambda: signed(payload(), timestamp="soon"), id="non-numeric-timestamp"),
    pytest.param(lambda: signed(payload(), timestamp="9" * 400), id="huge-timestamp"),
    pytest.param(lambda: signed(payload(), timestamp="+" + str(int(time.time()))), id="signed-timestamp"),
])
async def test_bad_or_stale_signature_is_rejected(site, request_kwargs):
    resp = await site.post("/ingest", **request_kwargs())
    assert resp.status == 401
    assert site.store.feed(10) == [] and site.received == []


@pytest.mark.parametrize("remote, header, status", [
    # E.g. from a container on a Docker network, when the site listens on 0.0.0.0.
    ("172.18.0.5", None, 404),
    ("203.0.113.9", None, 404),
    ("::ffff:192.0.2.6", None, 404),
    (None, None, 404),
    # Through a proxy, even one on this host.
    ("127.0.0.1", "X-Forwarded-For", 404),
    ("127.0.0.1", "Forwarded", 404),
    ("127.0.0.1", "CF-Connecting-IP", 404),
    ("127.0.0.1", None, 204),
    ("::1", None, 204),
    ("::ffff:127.0.0.1", None, 204),
])
async def test_only_direct_requests_from_loopback_are_taken(site, monkeypatch, remote, header, status):
    monkeypatch.setattr(web.Request, "remote", property(lambda self: remote))
    request = signed(payload())
    if header:
        request["headers"][header] = "for=203.0.113.9"
    assert (await site.post("/ingest", **request)).status == status
    assert len(site.received) == (status == 204)


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
    payload(monitor_version="<b>1</b>"),
    payload(monitor_version=1),
    pytest.param(b'{"v": 1, "id": "1", "severity": "info", "text": "\\ud800"}', id="lone-surrogate"),
    [1, 2],
])
async def test_malformed_payloads_are_rejected(site, data):
    assert (await site.post("/ingest", **signed(data))).status == 400
    assert site.store.feed(10) == []


def _db_error(message, code):
    exc = sqlite3.OperationalError(message)
    exc.sqlite_errorcode = code  # as SQLite sets it
    return exc


async def test_a_locked_database_is_busy_not_an_error(site, caplog):
    """E.g. an admin command holding the write lock past the busy timeout: the monitor retries a 503."""
    with patch.object(site.store, "add_notification", side_effect=_db_error("database is locked", sqlite3.SQLITE_BUSY)):
        resp = await site.post("/ingest", **signed(payload()))
    assert (resp.status, await resp.text(), resp.headers["Retry-After"]) == (503, "busy, try again", "2")
    assert "Database busy on POST /ingest: database is locked" in caplog.text
    assert (await site.post("/ingest", **signed(payload()))).status == 204  # and fine once it's free
    readonly = _db_error("attempt to write a readonly database", sqlite3.SQLITE_READONLY)
    with patch.object(site.store, "add_notification", side_effect=readonly):
        assert (await site.post("/ingest", **signed(payload(id="2")))).status == 500  # a fault, not busy

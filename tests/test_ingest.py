import sqlite3
import stat
import time
from unittest.mock import patch

import aiohttp
import pytest
from aiohttp import web

from push_site.__main__ import ingest_socket
from push_site.app import create_app
from tests.conftest import new_store
from tests.helpers import make_config, payload, signed


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


async def test_ingest_is_only_answered_over_the_ingest_socket(tmp_path, empty_db):
    """Through the real listeners: a signed notification over TCP (the public
    side, wherever it comes from) isn't found; over the socket it's taken."""
    store = new_store(tmp_path, empty_db)
    received = []
    runner = web.AppRunner(create_app(make_config(tmp_path), store, received.append, "BPublicKey"))
    await runner.setup()
    sock = ingest_socket(tmp_path / "ingest.sock")
    tcp = web.TCPSite(runner, "127.0.0.1", 0)
    await tcp.start()
    await web.SockSite(runner, sock).start()
    try:
        assert stat.S_IMODE((tmp_path / "ingest.sock").stat().st_mode) == 0o660
        port = tcp._server.sockets[0].getsockname()[1]
        async with aiohttp.ClientSession() as public:
            for path in ("/ingest", "/ingest/status"):
                assert (await public.post(f"http://127.0.0.1:{port}{path}", **signed(payload()))).status == 404
        async with aiohttp.ClientSession(connector=aiohttp.UnixConnector(str(tmp_path / "ingest.sock"))) as monitor:
            assert (await monitor.post("http://localhost/ingest", **signed(payload()))).status == 204
        assert [n["id"] for n in received] == ["0f0e-1"]
    finally:
        await runner.cleanup()
        store.close()


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

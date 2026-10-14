import pytest

from push_site.storage import DEVICE_IDLE_LIMIT
from tests.helpers import ORIGIN, api, signed_in


async def test_an_invite_is_redeemed_once_for_a_locked_down_device_cookie(site):
    invite = site.store.create_invite("Max's phone", uses=1, ttl=60)
    resp = await api(site, "POST", "/api/redeem", {"invite": invite})
    assert resp.status == 200
    cookie = resp.cookies["__Host-device"]
    assert cookie["secure"] and cookie["httponly"] and cookie["samesite"] == "Strict" and cookie["path"] == "/"
    assert int(cookie["max-age"]) == 365 * 86400

    resp = await api(site, "GET", "/api/me", token=cookie.value)
    assert resp.status == 200
    assert await resp.json() == {"label": "Max's phone"}
    assert resp.cookies["__Host-device"].value == cookie.value  # renewed
    assert resp.headers["Cache-Control"] == "no-store"

    assert (await api(site, "POST", "/api/redeem", {"invite": invite})).status == 403  # used up


@pytest.mark.parametrize("body", [
    {"invite": "nope"}, {"invite": ""}, {"invite": 5}, {},
    # Malformed: refused cleanly, not a 500.
    {"invite": "\ud800" * 43}, {"invite": "a" * 42}, {"invite": "a" * 44}, {"invite": "é" * 43},
])
async def test_bad_invites_are_refused(site, body):
    resp = await api(site, "POST", "/api/redeem", body)
    assert resp.status == 403
    assert "__Host-device" not in resp.cookies


@pytest.mark.parametrize("origin", [None, "https://evil.example", "http://push.example.org"])
async def test_changes_need_the_sites_own_origin(site, origin):
    invite = site.store.create_invite("a", uses=1, ttl=60)
    assert (await api(site, "POST", "/api/redeem", {"invite": invite}, origin=origin)).status == 403
    token = await signed_in(site)
    assert (await api(site, "DELETE", "/api/me", {}, token=token, origin=origin)).status == 403


@pytest.mark.parametrize("data, headers, status", [
    pytest.param("invite=INVITE", {"Content-Type": "application/x-www-form-urlencoded"}, 403, id="not-json"),
    pytest.param("", {}, 400, id="no-body"),
    pytest.param('{"invite": "INVITE"}', {"Content-Type": "application/json; charset=utf-8"}, 200, id="charset"),
    # Malformed: refused cleanly, not a 500.
    pytest.param("[" * 60000, {}, 400, id="deeply-nested"),
    pytest.param("{}", {"Content-Type": "application/json; charset=latin-9xx"}, 403, id="bad-charset"),
    pytest.param("not gzip", {"Content-Encoding": "gzip"}, 400, id="bad-encoding"),
])
async def test_changes_must_be_json(site, data, headers, status):
    invite = site.store.create_invite("a", uses=1, ttl=60)
    headers = {"Origin": ORIGIN, "Content-Type": "application/json", **headers}
    resp = await site.post("/api/redeem", data=data.replace("INVITE", invite).encode(), headers=headers)
    assert resp.status == status


@pytest.mark.parametrize("token", ["wrong", "\udcff" * 43, "x" * 5000])
async def test_api_needs_a_valid_device_cookie(site, token):
    assert (await api(site, "GET", "/api/me", token=token)).status == 401


async def test_idle_device_cookie_is_refused(site):
    token = await signed_in(site)
    site.store.db.execute("UPDATE devices SET last_seen = last_seen - ?", (DEVICE_IDLE_LIMIT,))
    site.store.db.commit()
    assert (await api(site, "GET", "/api/me", token=token)).status == 401


async def test_every_api_route_but_redeem_needs_a_device_cookie(site):
    routes = {(r.method, r.resource.canonical) for r in site.app.router.routes()
              if r.resource.canonical.startswith("/api/") and r.method != "HEAD"}
    assert routes  # the check below covers every route as more are added
    for method, path in routes - {("POST", "/api/redeem")}:
        resp = await api(site, method, path, None if method == "GET" else {})
        assert resp.status == 401, (method, path)
    assert (await site.head("/api/me")).status == 401
    assert (await site.options("/api/me")).status == 403


async def test_forget_me_removes_the_device(site):
    token = await signed_in(site)
    resp = await api(site, "DELETE", "/api/me", {}, token=token)
    assert resp.status == 204
    assert resp.cookies["__Host-device"].value == ""
    assert (await api(site, "GET", "/api/me", token=token)).status == 401
    assert site.store.devices() == []


async def test_every_response_has_security_headers(site):
    for resp in (await site.get("/api/me"), await site.get("/missing"), await site.post("/ingest")):
        assert "frame-ancestors 'none'" in resp.headers["Content-Security-Policy"]
        assert resp.headers["X-Content-Type-Options"] == "nosniff"
        assert resp.headers["Referrer-Policy"] == "same-origin"  # no-referrer would send Origin: null

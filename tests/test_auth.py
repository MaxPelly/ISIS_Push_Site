import pytest

from push_site.storage import DEVICE_IDLE_LIMIT
from tests.helpers import api, signed_in


async def test_redeem_sets_a_locked_down_device_cookie(site):
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


@pytest.mark.parametrize("body", [{"invite": "nope"}, {"invite": ""}, {"invite": 5}, {}])
async def test_bad_invites_are_refused(site, body):
    resp = await api(site, "POST", "/api/redeem", body)
    assert resp.status == 403
    assert "__Host-device" not in resp.cookies


async def test_used_invite_is_refused(site):
    invite = site.store.create_invite("a", uses=1, ttl=60)
    assert (await api(site, "POST", "/api/redeem", {"invite": invite})).status == 200
    assert (await api(site, "POST", "/api/redeem", {"invite": invite})).status == 403


@pytest.mark.parametrize("origin", [None, "https://evil.example", "http://push.example.org"])
async def test_changes_need_the_sites_own_origin(site, origin):
    invite = site.store.create_invite("a", uses=1, ttl=60)
    assert (await api(site, "POST", "/api/redeem", {"invite": invite}, origin=origin)).status == 403
    token = await signed_in(site)
    assert (await api(site, "DELETE", "/api/me", {}, token=token, origin=origin)).status == 403


async def test_changes_must_be_json(site):
    invite = site.store.create_invite("a", uses=1, ttl=60)
    resp = await site.post("/api/redeem", data=f"invite={invite}", headers={
        "Origin": "https://push.example.org", "Content-Type": "application/x-www-form-urlencoded"})
    assert resp.status == 403
    resp = await api(site, "POST", "/api/redeem", headers={"Content-Type": "application/json"})
    assert resp.status == 400  # no body


@pytest.mark.parametrize("token", [None, "wrong"])
async def test_api_needs_a_device_cookie(site, token):
    assert (await api(site, "GET", "/api/me", token=token)).status == 401


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


async def test_every_api_route_but_redeem_needs_a_device_cookie(site):
    routes = {(r.method, r.resource.canonical) for r in site.app.router.routes()
              if r.resource.canonical.startswith("/api/") and r.method != "HEAD"}
    assert routes  # the check below covers every route as more are added
    for method, path in routes - {("POST", "/api/redeem")}:
        resp = await api(site, method, path, None if method == "GET" else {})
        assert resp.status == 401, (method, path)


@pytest.mark.parametrize("invite", ["\ud800" * 43, "a" * 42, "a" * 44, "é" * 43])
async def test_malformed_invites_are_refused_cleanly(site, invite):
    assert (await api(site, "POST", "/api/redeem", {"invite": invite})).status == 403


async def test_malformed_cookies_are_refused_cleanly(site):
    for cookie in ("\udcff" * 43, "x" * 5000):
        assert (await api(site, "GET", "/api/me", token=cookie)).status == 401


@pytest.mark.parametrize("data, headers", [
    (b"[" * 60000, {}),
    (b"{}", {"Content-Type": "application/json; charset=latin-9xx"}),
    (b"not gzip", {"Content-Encoding": "gzip"}),
])
async def test_malformed_bodies_are_refused_cleanly(site, data, headers):
    headers = {"Origin": "https://push.example.org", "Content-Type": "application/json", **headers}
    assert (await site.post("/api/redeem", data=data, headers=headers)).status in (400, 403)


async def test_json_with_a_charset_is_accepted(site):
    invite = site.store.create_invite("a", uses=1, ttl=60)
    resp = await site.post("/api/redeem", data=f'{{"invite": "{invite}"}}', headers={
        "Origin": "https://push.example.org", "Content-Type": "application/json; charset=utf-8"})
    assert resp.status == 200


async def test_head_and_options_are_guarded(site):
    assert (await site.head("/api/me")).status == 401
    assert (await site.options("/api/me")).status == 403


async def test_idle_device_cookie_is_refused(site):
    token = await signed_in(site)
    site.store.db.execute("UPDATE devices SET last_seen = last_seen - ?", (DEVICE_IDLE_LIMIT,))
    site.store.db.commit()
    assert (await api(site, "GET", "/api/me", token=token)).status == 401

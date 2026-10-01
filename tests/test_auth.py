import pytest

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
        assert resp.headers["Referrer-Policy"] == "no-referrer"

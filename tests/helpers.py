import hashlib
import hmac
import json
import time

from push_site.config import Config

SECRET = b"k" * 32


def make_config(tmp_path, **overrides) -> Config:
    values = dict(public_origin="https://push.example.org", ingest_secret=SECRET, db_path=str(tmp_path / "push.db"))
    values.update(overrides)
    return Config(**values)


def payload(**overrides) -> dict:
    data = {
        "v": 1, "id": "0f0e-1", "title": "TS1 ⬇️ high → off", "text": "TS1 beam moved from high to off.",
        "summary": "TS1 ⬇️ high → off | ...", "severity": "attention", "emoji": "🔴",
        "facts": [["Current", "0.000 uA"]], "flavour": "", "url": None, "url_label": "Open",
        "timestamp": "2026-01-02T03:04:00+00:00", "channel": "TS1", "topic": "TS1",
    }
    data.update(overrides)
    return data


def signed(data, secret: bytes = SECRET, timestamp=None) -> dict:
    """Keyword arguments for client.post() sending `data` signed like the daemon."""
    # Raw UTF-8, as the monitor sends it (ensure_ascii=False).
    body = data if isinstance(data, bytes) else json.dumps(data, ensure_ascii=False).encode()
    ts = str(int(time.time()) if timestamp is None else timestamp)
    sig = hmac.new(secret, ts.encode() + b"." + body, hashlib.sha256).hexdigest()
    return {"data": body, "headers": {"Content-Type": "application/json", "X-Timestamp": ts, "X-Signature": sig}}


ORIGIN = "https://push.example.org"


async def api(client, method: str, path: str, body=None, token=None, origin=ORIGIN, headers=None):
    """Call the JSON API like the site's own pages do."""
    headers = dict(headers or {})
    if origin:
        headers["Origin"] = origin
    if token:
        headers["Cookie"] = f"__Host-device={token}"
    if body is not None:
        return await client.request(method, path, json=body, headers=headers)
    return await client.request(method, path, headers=headers)


async def signed_in(client) -> str:
    """A device token, from redeeming a fresh invite."""
    invite = client.store.create_invite("test", uses=1, ttl=60)
    resp = await api(client, "POST", "/api/redeem", {"invite": invite})
    assert resp.status == 200
    return resp.cookies["__Host-device"].value

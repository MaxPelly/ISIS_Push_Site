import json
from unittest.mock import patch

import pytest

from push_site import app as site_app
from push_site.status import MAX_INSTRUMENTS, NEWS, TITLE, parse_status
from tests.helpers import api, signed, signed_in, status


def test_a_full_status_parses_to_the_same():
    assert parse_status(json.loads(json.dumps(status()))) == {**status(), "interval_s": 60.0}


@pytest.mark.parametrize("change, match", [
    ({"v": 2}, "expected version 1"),
    ({"kind": "notification"}, 'kind: expected "status"'),
    ({"generated_at": "yesterday"}, "generated_at"),
    ({"generated_at": "2026-10-13T10:31:30"}, "with a zone"),
    ({"interval_s": 0}, "interval_s"),
    ({"health": {"x" * 17: "connected"}}, "health"),
    ({"beams": [{}] * 4}, "at most 3"),
    ({"beams": [{**status()["beams"][0], "current": float("nan")}]}, "beams: current"),
    ({"beams": [{**status()["beams"][0], "history": {"interval_s": 600, "values": [[1.0]]}}]}, "current, power"),
    ({"instruments": [{**status()["instruments"][0], "title": "x" * (TITLE + 1)}]}, "title"),
    ({"instruments": [{**status()["instruments"][0], "auto": "no"}]}, "auto"),
    ({"instruments": [{**status()["instruments"][0], "counts": True}]}, "counts"),
    ({"instruments": status()["instruments"] * (MAX_INSTRUMENTS + 1)}, "at most 64"),
    ({"mcr": {"news": "x" * (NEWS + 1), "seen_at": None}}, "news"),
    ({"mcr": {"news": "\ud800", "seen_at": None}}, "surrogates|encode"),
])
def test_a_bad_status_is_refused(change, match):
    with pytest.raises(ValueError, match=match):
        parse_status({**status(), **change})


def test_extra_fields_are_dropped():
    data = status(extra="x")
    data["instruments"][0]["secret"] = "y"
    parsed = parse_status(data)
    assert "extra" not in parsed and "secret" not in parsed["instruments"][0]


def test_a_long_status_fits_the_request_limit():
    """20 instruments (far more than are watched) with every string at its longest."""
    long = status(
        instruments=[{**status()["instruments"][0], "name": "N" * 32, "title": "é" * TITLE}] * 20,
        mcr={"news": "😀" * NEWS, "seen_at": None},
        beams=[{**status()["beams"][0], "history": {"interval_s": 600, "values": [[150.0, "high"]] * 200}}] * 3,
    )
    assert len(json.dumps(parse_status(long), ensure_ascii=False).encode()) < site_app.MAX_BODY


async def test_the_newest_status_is_kept_and_served(site):
    token = await signed_in(site)
    assert await (await api(site, "GET", "/api/status", token=token)).json() == {"status": None}
    assert (await site.post("/ingest/status", **signed(status()))).status == 204
    body = await (await api(site, "GET", "/api/status", token=token)).json()
    assert body["status"]["instruments"][0]["name"] == "PEARL" and body["stale"] is False
    # An older or repeated one (a replay inside the signature's window) is refused.
    for older in ("2026-10-13T10:31:30+00:00", "2026-10-13T11:31:00+01:00"):
        assert (await site.post("/ingest/status", **signed(status(generated_at=older, mcr={"news": "old",
                                                                                         "seen_at": None})))).status == 409
    newer = status(generated_at="2026-10-13T11:32:30+01:00")  # a minute on, in another zone
    assert (await site.post("/ingest/status", **signed(newer))).status == 204
    assert (await (await api(site, "GET", "/api/status", token=token)).json())["status"]["generated_at"] == \
        newer["generated_at"]


async def test_a_status_survives_a_restart(site, tmp_path):
    await site.post("/ingest/status", **signed(status()))
    site.store.close()
    from push_site.storage import Store
    reopened = Store(tmp_path / "push.db")
    assert reopened.status()[0] == parse_status(status())
    reopened.close()


async def test_a_status_not_refreshed_for_three_intervals_is_stale(site):
    token = await signed_in(site)
    await site.post("/ingest/status", **signed(status(interval_s=60)))
    with patch.object(site_app.time, "time", return_value=site.store.status()[1] + 181):
        body = await (await api(site, "GET", "/api/status", token=token)).json()
    assert body["stale"] is True


async def test_status_needs_signing_in_and_a_signature(site):
    assert (await api(site, "GET", "/api/status")).status == 401
    assert (await site.post("/ingest/status", **signed(status(), secret=b"x" * 32))).status == 401
    forwarded = signed(status())
    forwarded["headers"]["X-Forwarded-For"] = "1.2.3.4"
    assert (await site.post("/ingest/status", **forwarded)).status == 404
    assert (await site.post("/ingest/status", **signed(status(v=2)))).status == 400
    assert site.store.status() is None


@pytest.mark.parametrize("change", [{"v": True}, {"v": 1.0}, {"interval_s": 10 ** 400}])
def test_odd_numbers_are_refused_cleanly(change):
    with pytest.raises(ValueError):
        parse_status({**status(), **change})


async def test_a_status_from_the_future_is_refused(site):
    """Kept, it would hold back every status until the monitor's clock caught up."""
    resp = await site.post("/ingest/status", **signed(status(generated_at="2099-01-01T00:00:00+00:00")))
    assert resp.status == 400 and site.store.status() is None


async def test_a_repeated_rejection_is_warned_of_once(site, caplog):
    for _ in range(2):
        await site.post("/ingest/status", **signed(status(v=2)))
    await site.post("/ingest/status", **signed(status()))
    await site.post("/ingest/status", **signed(status(v=2)))
    assert caplog.text.count("WARNING") == 2  # and again after a good one


async def test_the_status_age_is_by_the_sites_clock(site):
    token = await signed_in(site)
    await site.post("/ingest/status", **signed(status()))
    with patch.object(site_app.time, "time", return_value=site.store.status()[1] + 42):
        body = await (await api(site, "GET", "/api/status", token=token)).json()
    assert body["age"] == pytest.approx(42) and body["stale"] is False

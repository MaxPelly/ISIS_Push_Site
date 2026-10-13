from datetime import datetime, timezone

import pytest

from push_site.app import CONFIG
from push_site.prefs import DEFAULT_PREFS, parse_prefs, wants
from tests.helpers import api, payload, signed, signed_in

QUIET = {"start": "22:00", "end": "07:00", "timezone": "Europe/London", "warnings_only": True}


def at(hour, minute=0):  # a summer UTC time; London is UTC+1
    return datetime(2026, 7, 1, hour - 1, minute, tzinfo=timezone.utc)


def test_defaults_to_the_fixed_topics_without_quiet_hours():
    assert DEFAULT_PREFS == {"topics": ["TS1", "TS2", "Muons", "MCR", "Summary"], "quiet": None}


def test_topic_filter():
    prefs = parse_prefs({"topics": ["TS1", "PEARL", "TS1"], "quiet": None})
    assert prefs["topics"] == ["PEARL", "TS1"]
    assert wants(prefs, {"topic": "PEARL", "severity": "info"})
    assert not wants(prefs, {"topic": "TS2", "severity": "attention"})


@pytest.mark.parametrize("when, severity, warnings_only, sent", [
    (at(21, 59), "info", True, True),
    (at(22, 0), "info", True, False),
    (at(3), "info", True, False),
    (at(6, 59), "info", True, False),
    (at(7, 0), "info", True, True),
    (at(3), "attention", True, True),   # warnings still come through
    (at(3), "warning", True, True),
    (at(3), "attention", False, False),  # unless everything is muted
])
def test_quiet_hours_across_midnight(when, severity, warnings_only, sent):
    prefs = parse_prefs({"topics": ["TS1"], "quiet": {**QUIET, "warnings_only": warnings_only}})
    assert wants(prefs, {"topic": "TS1", "severity": severity}, when) is sent


def test_quiet_hours_within_a_day():
    prefs = parse_prefs({"topics": ["TS1"], "quiet": {**QUIET, "start": "09:00", "end": "17:00"}})
    assert not wants(prefs, {"topic": "TS1", "severity": "info"}, at(12))
    assert wants(prefs, {"topic": "TS1", "severity": "info"}, at(18))


@pytest.mark.parametrize("data", [
    [], {"quiet": None}, {"topics": "TS1", "quiet": None}, {"topics": [""], "quiet": None},
    {"topics": ["x" * 65], "quiet": None}, {"topics": ["t"] * 201, "quiet": None},
    {"topics": [], "quiet": {**QUIET, "start": "7:00"}},
    {"topics": [], "quiet": {**QUIET, "start": "25:00"}},
    {"topics": [], "quiet": {**QUIET, "start": "+1:00"}},
    {"topics": [], "quiet": {**QUIET, "start": "٠١:00"}},
    {"topics": [], "quiet": {**QUIET, "end": "22:00"}},
    {"topics": [], "quiet": {**QUIET, "timezone": "Nowhere/City"}},
    {"topics": [], "quiet": {**QUIET, "timezone": "../../etc/passwd"}},
    {"topics": [], "quiet": {**QUIET, "timezone": None}},
    {"topics": [], "quiet": {**QUIET, "warnings_only": "yes"}},
])
def test_bad_prefs_are_refused(data):
    with pytest.raises(ValueError):
        parse_prefs(data)


async def test_prefs_api(site):
    token = await signed_in(site)
    assert await (await api(site, "GET", "/api/prefs", token=token)).json() == DEFAULT_PREFS
    new = {"topics": ["PEARL"], "quiet": {**QUIET, "extra": 1}}
    assert (await api(site, "PUT", "/api/prefs", new, token=token)).status == 204
    assert await (await api(site, "GET", "/api/prefs", token=token)).json() == {"topics": ["PEARL"], "quiet": QUIET}
    resp = await api(site, "PUT", "/api/prefs", {"topics": 5}, token=token)
    assert resp.status == 400 and await resp.text() == "bad topics"


async def test_topics_and_feed_api(site):
    token = await signed_in(site)
    for i, topic in enumerate(["PEARL", "TS1", "WISH"]):
        await site.post("/ingest", **signed(payload(id=f"{i}", topic=topic)))
    topics = await (await api(site, "GET", "/api/topics", token=token)).json()
    assert topics == ["TS1", "TS2", "Muons", "MCR", "Summary", "PEARL", "WISH"]
    feed = await (await api(site, "GET", "/api/feed?limit=2", token=token)).json()
    assert [n["id"] for n in feed] == ["2", "1"]
    assert (await api(site, "GET", "/api/feed?limit=lots", token=token)).status == 400
    assert len(await (await api(site, "GET", "/api/feed?limit=-5", token=token)).json()) == 0
    everything = await (await api(site, "GET", "/api/feed", token=token)).json()
    assert [n["id"] for n in everything] == ["2", "1", "0"]


async def test_feed_api_returns_every_kind_kept_and_no_more(site, monkeypatch):
    token = await signed_in(site)
    monkeypatch.setattr(site.app[CONFIG], "feed_items_per_kind", 2)
    for i, topic in enumerate(["MCR", "MCR", "MCR", "Summary", "TS1", "TS2", "PEARL", "WISH", "ZOOM"]):
        await site.post("/ingest", **signed(payload(id=f"{i}", topic=topic)))
    feed = await (await api(site, "GET", "/api/feed", token=token)).json()
    assert [n["id"] for n in feed] == ["8", "7", "5", "4", "3", "2", "1"]
    capped = await (await api(site, "GET", "/api/feed?limit=1000", token=token)).json()
    assert len(capped) == 7


def test_quiet_hours_follow_the_clock_across_dst():
    prefs = parse_prefs({"topics": ["TS1"], "quiet": {**QUIET, "warnings_only": False}})
    n = {"topic": "TS1", "severity": "info"}
    # 22:30 local both sides of the October change: UTC+1, then UTC+0.
    assert not wants(prefs, n, datetime(2026, 10, 24, 21, 30, tzinfo=timezone.utc))
    assert not wants(prefs, n, datetime(2026, 10, 25, 22, 30, tzinfo=timezone.utc))
    assert wants(prefs, n, datetime(2026, 10, 25, 21, 30, tzinfo=timezone.utc))  # 21:30 GMT

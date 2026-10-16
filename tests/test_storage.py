import json
import sqlite3
import stat

import pytest

from push_site.storage import DEVICE_IDLE_LIMIT


def test_db_file_is_owner_only(tmp_path, store):
    assert stat.S_IMODE((tmp_path / "push.db").stat().st_mode) == 0o600


def test_add_ignores_repeated_ids(store):
    assert store.add_notification({"id": "a", "title": "A"}, 10, 7, now=1)
    assert not store.add_notification({"id": "a", "title": "changed"}, 10, 7, now=2)
    assert store.feed(10) == [{"id": "a", "title": "A"}]


def test_feed_is_newest_first_and_pruned_by_count_and_age(store):
    day = 86400
    for i in range(5):
        store.add_notification({"id": str(i), "topic": "TS1"}, per_kind=3, max_days=7, now=i)
    assert [n["id"] for n in store.feed(10)] == ["4", "3", "2"]
    assert [n["id"] for n in store.feed(2)] == ["4", "3"]
    store.add_notification({"id": "late", "topic": "TS1"}, per_kind=3, max_days=1, now=4 + day + 1)
    assert [n["id"] for n in store.feed(10)] == ["late"]


def test_feed_keeps_the_newest_of_each_kind(store):
    """A burst of beam cards doesn't push MCR news, summaries or experiments out."""
    sent = [("mcr", "MCR"), ("sum", "Summary"), ("pearl", "PEARL"), ("wish", "WISH")]
    sent += [(f"beam{i}", ("TS1", "TS2", "Muons")[i % 3]) for i in range(10)]
    sent += [("mcr2", "MCR"), ("mcr3", "MCR")]
    for i, (id_, topic) in enumerate(sent):
        store.add_notification({"id": id_, "topic": topic}, per_kind=2, max_days=7, now=i)
    # TS1, TS2 and Muons share the beam kind; instruments share the experiment kind.
    assert [n["id"] for n in store.feed(100)] == ["mcr3", "mcr2", "beam9", "beam8", "wish", "pearl", "sum"]


def test_invite_redeems_for_its_uses_then_stops(store):
    invite = store.create_invite("Max's phone", uses=2, ttl=60, now=0)
    first, second = store.redeem_invite(invite, now=1), store.redeem_invite(invite, now=2)
    assert first and second and first != second
    assert store.redeem_invite(invite, now=3) is None
    assert [d["label"] for d in store.devices(now=3)] == ["Max's phone", "Max's phone"]
    assert store.db.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == 0  # used up and removed


def test_expired_or_unknown_invites_do_nothing(store):
    invite = store.create_invite("a", uses=1, ttl=60, now=0)
    assert store.redeem_invite(invite, now=60) is None
    assert store.redeem_invite("not-an-invite", now=1) is None
    assert store.devices() == []
    store.create_invite("b", uses=1, ttl=60, now=61)  # also clears out the expired one
    assert store.db.execute("SELECT COUNT(*) FROM invites").fetchone()[0] == 1


def test_tokens_are_stored_hashed(store):
    invite = store.create_invite("a", uses=1, ttl=60)
    token = store.redeem_invite(invite)
    dump = "\n".join(store.db.iterdump())
    assert invite not in dump and token not in dump


def test_device_lookup_tracks_last_seen_and_expires_when_idle(store):
    invite = store.create_invite("a", uses=1, ttl=60, now=0)
    token = store.redeem_invite(invite, now=0)
    device_id = store.device_for_token(token, now=10)
    assert device_id is not None
    assert store.devices(now=10)[0]["last_seen"] == 0  # not rewritten within the hour
    assert store.device_for_token(token, now=7200) == device_id
    assert store.devices(now=7200)[0]["last_seen"] == 7200
    assert store.device_for_token(token, now=7200 + DEVICE_IDLE_LIMIT) is None
    assert store.device_for_token("wrong", now=7200) is None
    # Idle devices are deleted when listed (or when an invite is made).
    assert store.devices(now=7200 + DEVICE_IDLE_LIMIT) == []


def test_delete_device(store):
    store.redeem_invite(store.create_invite("a", uses=1, ttl=60))
    device_id = store.devices()[0]["id"]
    assert store.delete_device(device_id)
    assert not store.delete_device(device_id)
    assert store.devices() == []


def test_the_status_is_saved_at_most_once_a_minute_and_on_close(tmp_path, store):
    from tests.helpers import status

    def saved():
        row = store.db.execute("SELECT payload FROM status").fetchone()
        return json.loads(row["payload"])["generated_at"] if row else None

    times = [f"2026-10-13T10:31:{s:02d}+00:00" for s in (0, 5, 10)]
    assert store.set_status(status(generated_at=times[0]), now=100) and saved() == times[0]
    assert store.set_status(status(generated_at=times[1]), now=105)
    assert saved() == times[0] and store.status()[0]["generated_at"] == times[1]  # served from memory
    assert not store.set_status(status(generated_at=times[0]), now=106)  # not newer than the one in memory
    assert store.set_status(status(generated_at=times[2]), now=160) and saved() == times[2]  # a minute on
    store.set_status(status(generated_at="2026-10-13T10:32:00+00:00"), now=170)
    store.close()
    from push_site.storage import Store
    reopened = Store(tmp_path / "push.db")  # the last was saved on close
    assert reopened.status()[0]["generated_at"] == "2026-10-13T10:32:00+00:00"
    reopened.close()


def test_a_status_that_cant_be_saved_is_still_taken_and_tried_again_a_minute_on(tmp_path, store, caplog):
    from tests.helpers import status
    store.db.close()
    store.db = sqlite3.connect(f"file:{tmp_path / 'push.db'}?mode=ro", uri=True)  # e.g. a full or read-only disk
    for now in (100, 105, 160):
        assert store.set_status(status(generated_at=f"2026-10-13T10:{now // 60:02d}:{now % 60:02d}+00:00"), now=now)
    assert caplog.text.count("Couldn't save the status") == 1
    assert store._status_saved_at == 160  # tried again a minute on, not at 105
    store.close()  # still unsaved, and closed regardless
    with pytest.raises(sqlite3.ProgrammingError):
        store.db.execute("SELECT 1")

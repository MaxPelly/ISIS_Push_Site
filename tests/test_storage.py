import stat

import pytest

from push_site.storage import DEVICE_IDLE_LIMIT, Store


@pytest.fixture
def store(tmp_path):
    store = Store(tmp_path / "push.db")
    yield store
    store.close()


def test_db_file_is_owner_only(tmp_path, store):
    assert stat.S_IMODE((tmp_path / "push.db").stat().st_mode) == 0o600


def test_add_ignores_repeated_ids(store):
    assert store.add_notification({"id": "a", "title": "A"}, 10, 7, now=1)
    assert not store.add_notification({"id": "a", "title": "changed"}, 10, 7, now=2)
    assert store.feed(10) == [{"id": "a", "title": "A"}]


def test_feed_is_newest_first_and_pruned_by_count_and_age(store):
    day = 86400
    for i in range(5):
        store.add_notification({"id": str(i)}, max_items=3, max_days=7, now=i)
    assert [n["id"] for n in store.feed(10)] == ["4", "3", "2"]
    assert [n["id"] for n in store.feed(2)] == ["4", "3"]
    store.add_notification({"id": "late"}, max_items=3, max_days=1, now=4 + day + 1)
    assert [n["id"] for n in store.feed(10)] == ["late"]


def test_invite_redeems_for_its_uses_then_stops(store):
    invite = store.create_invite("Max's phone", uses=2, ttl=60, now=0)
    first, second = store.redeem_invite(invite, now=1), store.redeem_invite(invite, now=2)
    assert first and second and first != second
    assert store.redeem_invite(invite, now=3) is None
    assert [d["label"] for d in store.devices()] == ["Max's phone", "Max's phone"]
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
    assert store.devices()[0]["last_seen"] == 0  # not rewritten within the hour
    assert store.device_for_token(token, now=7200) == device_id
    assert store.devices()[0]["last_seen"] == 7200
    assert store.device_for_token(token, now=7200 + DEVICE_IDLE_LIMIT) is None
    assert store.device_for_token("wrong", now=7200) is None


def test_delete_device(store):
    store.redeem_invite(store.create_invite("a", uses=1, ttl=60))
    device_id = store.devices()[0]["id"]
    assert store.delete_device(device_id)
    assert not store.delete_device(device_id)
    assert store.devices() == []

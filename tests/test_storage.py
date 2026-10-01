import stat

import pytest

from push_site.storage import Store


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

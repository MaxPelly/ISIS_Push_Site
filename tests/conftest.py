import shutil

import pytest

from push_site.app import create_app
from push_site.storage import Store
from tests.helpers import make_config


@pytest.fixture(scope="session")
def empty_db(tmp_path_factory):
    """A new database, made once: creating the schema (with its fsyncs) is most of a test's time."""
    path = tmp_path_factory.mktemp("template") / "push.db"
    Store(path).close()
    return path


def new_store(tmp_path, empty_db) -> Store:
    shutil.copy(empty_db, tmp_path / "push.db")  # keeps its owner-only mode
    return Store(tmp_path / "push.db")


@pytest.fixture
def store(tmp_path, empty_db):
    store = new_store(tmp_path, empty_db)
    yield store
    store.close()


@pytest.fixture
async def site(tmp_path, empty_db, aiohttp_client):
    """A test client for the app; `received` collects what's passed on for fan-out."""
    store = new_store(tmp_path, empty_db)
    received = []

    client = await aiohttp_client(create_app(make_config(tmp_path), store, received.append, "BPublicKey"))
    client.received, client.store = received, store
    yield client
    store.close()

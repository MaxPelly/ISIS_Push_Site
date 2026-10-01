import pytest

from push_site.app import create_app
from push_site.storage import Store
from tests.helpers import make_config


@pytest.fixture
async def site(tmp_path, aiohttp_client):
    """A test client for the app; `received` collects what's passed on for fan-out."""
    store = Store(tmp_path / "push.db")
    received = []

    async def on_notification(n):
        received.append(n)
    client = await aiohttp_client(create_app(make_config(tmp_path), store, on_notification))
    client.received, client.store = received, store
    yield client
    store.close()

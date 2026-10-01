import pytest

from push_site.config import ConfigError, load_config

SECRET = "s" * 64


@pytest.fixture
def write(tmp_path):
    secret_file = tmp_path / "push.secret"
    secret_file.write_text(SECRET + "\n")
    secret_file.chmod(0o600)

    def write(body: str = "", origin: str = "https://push.example.org", secret=secret_file):
        path = tmp_path / "push_site.ini"
        # `body` continues the [SERVER] section.
        path.write_text(f"[SECRETS]\ningest_secret_file = {secret}\n[SERVER]\npublic_origin = {origin}\n{body}")
        return path
    return write


def test_defaults_and_secret(write, tmp_path):
    config = load_config(write())
    assert (config.host, config.port) == ("127.0.0.1", 8765)
    assert config.db_path == str(tmp_path / "push_site.db")  # relative to the config file
    assert (config.feed_max_items, config.feed_max_days) == (200, 7.0)
    assert config.public_origin == "https://push.example.org"
    assert config.ingest_secret == SECRET.encode()
    assert SECRET not in repr(config)


def test_values_are_read(write):
    config = load_config(write("[STORAGE]\nfeed_max_items = 60\nfeed_max_days = 1.5\n", origin="https://X.org:8443/"))
    assert (config.feed_max_items, config.feed_max_days, config.public_origin) == (60, 1.5, "https://x.org:8443")


@pytest.mark.parametrize("kwargs, match", [
    ({"origin": ""}, "public_origin"),
    ({"origin": "http://push.example.org"}, "public_origin"),
    ({"origin": "https://push.example.org/app"}, "public_origin"),
    ({"origin": "https://user:pw@push.example.org"}, "public_origin"),
    ({"origin": "https://[abc"}, "public_origin"),
    ({"origin": "https://push.example.org:99999"}, "public_origin"),
    ({"body": "[STORAGE]\nfeed_max_items = 10\n"}, "feed_max_items must be between 50"),
    ({"secret": ""}, "ingest_secret_file is required"),
    ({"secret": "/nonexistent"}, "could not be read"),
    ({"body": "port = 0\n"}, "port must be between"),
    ({"body": "[STORAGE]\nfeed_max_days = nan\n"}, "feed_max_days must be between"),
    ({"body": "[STORAGE]\nfeed_max_items = lots\n"}, r"\[STORAGE\] feed_max_items"),
])
def test_invalid_config(write, kwargs, match):
    body = kwargs.pop("body", "")
    with pytest.raises(ConfigError, match=match):
        load_config(write(body, **kwargs))


@pytest.mark.parametrize("secret", ["short", "x" * 5000])
def test_secret_length_is_checked(write, tmp_path, secret):
    (tmp_path / "push.secret").write_text(secret)
    with pytest.raises(ConfigError, match="must hold"):
        load_config(write())


def test_world_readable_secret_warns(write, tmp_path, caplog):
    (tmp_path / "push.secret").chmod(0o644)
    load_config(write())
    assert "world-readable" in caplog.text


def test_missing_file(tmp_path):
    with pytest.raises(ConfigError, match="Could not read"):
        load_config(tmp_path / "nope.ini")


def test_default_port_is_dropped_from_the_origin(write):
    assert load_config(write(origin="https://Push.Example.org:443")).public_origin == "https://push.example.org"

import pytest

from push_site.__main__ import main
from push_site.config import ConfigError
from push_site.push import load_vapid_key, public_key
from push_site.storage import Store


@pytest.fixture
def ini(tmp_path):
    secret = tmp_path / "push.secret"
    secret.write_text("s" * 32)
    secret.chmod(0o600)
    path = tmp_path / "push_site.ini"
    path.write_text(f"[SERVER]\npublic_origin = https://push.example.org\n[SECRETS]\ningest_secret_file = {secret}\n"
                    "vapid_subject = mailto:admin@example.org\n"
                    f"[STORAGE]\ndb_path = {tmp_path / 'push.db'}\n")
    return path


def test_invite_devices_and_revoke(ini, tmp_path, capsys):
    main(["invite", str(ini), "--label", "Max's phone", "--uses", "2", "--days", "1.5"])
    out = capsys.readouterr().out
    assert "usable 2 times within 1.5 days" in out
    link = out.strip().splitlines()[-1]
    assert link.startswith("https://push.example.org/#invite=")

    store = Store(tmp_path / "push.db")
    assert store.redeem_invite(link.split("#invite=")[1])
    store.close()

    main(["devices", str(ini)])
    assert "Max's phone" in capsys.readouterr().out
    main(["revoke", str(ini), "1"])
    assert "revoked" in capsys.readouterr().out
    with pytest.raises(SystemExit, match="No device 1"):
        main(["revoke", str(ini), "1"])
    main(["devices", str(ini)])
    assert "No devices." in capsys.readouterr().out


@pytest.mark.parametrize("args", [["--uses", "0"], ["--days", "-1"], ["--days", "nan"], []])
def test_invite_arguments_are_checked(ini, args):
    with pytest.raises(SystemExit):
        main(["invite", str(ini), *args] + (["--label", "x"] if args else []))


def test_config_error_is_reported(tmp_path):
    with pytest.raises(SystemExit, match="Configuration error"):
        main(["devices", str(tmp_path / "missing.ini")])


def test_refuses_to_run_as_root(ini, monkeypatch):
    monkeypatch.setattr("os.geteuid", lambda: 0)
    with pytest.raises(SystemExit, match="not root"):
        main(["devices", str(ini)])


def test_vapid_creates_a_private_key_once(ini, tmp_path, capsys):
    main(["vapid", str(ini)])
    key_file = tmp_path / "vapid.pem"
    assert oct(key_file.stat().st_mode & 0o777) == "0o600"
    public = capsys.readouterr().out.strip().splitlines()[-1]
    assert public == public_key(load_vapid_key(key_file))
    assert len(public) == 87  # base64url of a 65-byte uncompressed P-256 point
    with pytest.raises(SystemExit, match="already exists"):
        main(["vapid", str(ini)])


def test_bad_vapid_key_is_a_config_error(tmp_path):
    (tmp_path / "bad.pem").write_text("nope")
    with pytest.raises(ConfigError, match="not a valid VAPID key"):
        load_vapid_key(tmp_path / "bad.pem")
    with pytest.raises(ConfigError, match="python -m push_site vapid"):
        load_vapid_key(tmp_path / "missing.pem")


def test_vapid_doesnt_open_the_database(ini, tmp_path, monkeypatch):
    """It's run by the directory's owner, who can't enter the site user's data/."""
    monkeypatch.setattr("push_site.__main__.Store", lambda path: pytest.fail("opened the database"))
    main(["vapid", str(ini)])
    assert (tmp_path / "vapid.pem").exists()

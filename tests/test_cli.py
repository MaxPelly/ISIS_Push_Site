import pytest

from push_site.__main__ import main
from push_site.storage import Store


@pytest.fixture
def ini(tmp_path):
    secret = tmp_path / "push.secret"
    secret.write_text("s" * 32)
    secret.chmod(0o600)
    path = tmp_path / "push_site.ini"
    path.write_text(f"[SERVER]\npublic_origin = https://push.example.org\n[SECRETS]\ningest_secret_file = {secret}\n"
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

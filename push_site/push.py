"""Web Push: the site's VAPID key."""
import base64
import os
from pathlib import Path

from cryptography.hazmat.primitives import serialization
from py_vapid import Vapid02

from push_site.config import ConfigError


def generate_vapid_key(path: Path) -> Vapid02:
    """Create a new VAPID private key file (owner-only); never overwrites one."""
    vapid = Vapid02()
    vapid.generate_keys()
    try:
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
    except FileExistsError:
        raise ConfigError(f"{path} already exists; delete it first to replace the key "
                          "(every device will then have to subscribe again)")
    with os.fdopen(fd, "wb") as f:
        f.write(vapid.private_pem())
    return vapid


def load_vapid_key(path: Path) -> Vapid02:
    try:
        return Vapid02.from_pem(path.read_bytes())
    except OSError as exc:
        raise ConfigError(f"Could not read the VAPID key ({exc}); create one with `python -m push_site vapid`")
    except Exception as exc:  # whatever cryptography raises for a malformed PEM
        raise ConfigError(f"{path} is not a valid VAPID key: {exc}")


def public_key(vapid: Vapid02) -> str:
    """The key browsers subscribe with (applicationServerKey), base64url."""
    raw = vapid.public_key.public_bytes(serialization.Encoding.X962, serialization.PublicFormat.UncompressedPoint)
    return base64.urlsafe_b64encode(raw).rstrip(b"=").decode()

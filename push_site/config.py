"""Loading and validating push_site.ini."""
import configparser
import logging
from dataclasses import dataclass, field, fields
from pathlib import Path
from urllib.parse import urlsplit

logger = logging.getLogger(__name__)

MIN_SECRET_BYTES = 32
MAX_SECRET_BYTES = 4096


class ConfigError(Exception):
    """Raised when the configuration is missing or invalid."""


def _ini(section: str, default, key: str = ""):
    return field(default=default, metadata={"section": section, "key": key})


@dataclass
class Config:
    host: str = _ini("SERVER", "127.0.0.1")
    port: int = _ini("SERVER", 8765)
    # The site's public origin, e.g. https://push.example.org: same-origin checks need it.
    public_origin: str = _ini("SERVER", "")

    ingest_secret_file: str = _ini("SECRETS", "")
    ingest_secret: bytes = field(default=b"", repr=False)  # read from ingest_secret_file
    # Created by `python -m push_site vapid`; identifies this site to push services.
    vapid_key_file: str = _ini("SECRETS", "vapid.pem")
    # How push services can contact the site's admin: mailto: or https: (required).
    vapid_subject: str = _ini("SECRETS", "")

    db_path: str = _ini("STORAGE", "push_site.db")
    # The feed keeps this many of each kind of notification (see prefs.FEED_KINDS).
    feed_items_per_kind: int = _ini("STORAGE", 75)
    feed_max_days: float = _ini("STORAGE", 30.0)


# A repeated id is only refused while it's still in the feed, so the feed must
# outlast /ingest's 60s timestamp window: at least 10 of each kind and 864s.
_BOUNDS = {"port": (1, 65535), "feed_items_per_kind": (10, 1000), "feed_max_days": (0.01, 365)}


def read_secret(path: Path, what: str) -> bytes:
    """A shared secret from a file, with surrounding whitespace dropped."""
    try:
        with path.open("rb") as f:
            secret = f.read(MAX_SECRET_BYTES + 1).strip()
        world_readable = path.stat().st_mode & 0o004
    except OSError as exc:
        raise ConfigError(f"{what} could not be read: {exc}") from exc
    if not MIN_SECRET_BYTES <= len(secret) <= MAX_SECRET_BYTES:
        raise ConfigError(f"{what} must hold {MIN_SECRET_BYTES} to {MAX_SECRET_BYTES} characters")
    if world_readable:
        logger.warning(f"{what} {path} is world-readable; consider chmod o-r")
    return secret


def load_config(path: Path) -> Config:
    parser = configparser.ConfigParser(interpolation=None)
    try:
        with path.open(encoding="utf-8") as f:
            parser.read_file(f)
    except (OSError, UnicodeDecodeError) as exc:
        raise ConfigError(f"Could not read {path}: {exc}") from exc
    except configparser.Error as exc:
        raise ConfigError(f"Could not parse {path}: {exc}") from exc

    values = {}
    for f in fields(Config):
        if "section" not in f.metadata:
            continue
        section, key = f.metadata["section"], f.metadata["key"] or f.name
        raw = parser.get(section, key, fallback="").strip()
        if not raw:
            continue
        try:
            values[f.name] = f.type(raw)
        except ValueError as exc:
            raise ConfigError(f"[{section}] {key}: {exc}") from exc
    config = Config(**values)

    for name, (low, high) in _BOUNDS.items():
        if not low <= getattr(config, name) <= high:  # also rejects nan
            raise ConfigError(f"{name} must be between {low} and {high}")
    try:
        origin = urlsplit(config.public_origin)
        _ = origin.port  # raises ValueError if out of range
    except ValueError:
        origin = None
    if (not origin or origin.scheme != "https" or not origin.hostname or "@" in origin.netloc
            or origin.path not in ("", "/") or origin.query or origin.fragment):
        raise ConfigError("[SERVER] public_origin must be an https:// origin, e.g. https://push.example.org")
    # As browsers send it in the Origin header: lower-case, no default port.
    netloc = origin.netloc.lower()
    config.public_origin = f"https://{netloc[:-4] if netloc.endswith(':443') else netloc}"
    if not config.ingest_secret_file:
        raise ConfigError("[SECRETS] ingest_secret_file is required")
    # Relative paths are relative to the config file, so the admin commands
    # find the same files wherever they're run from.
    config.ingest_secret_file = str(path.parent / config.ingest_secret_file)
    config.db_path = str(path.parent / config.db_path)
    config.vapid_key_file = str(path.parent / config.vapid_key_file)
    if not config.vapid_subject.startswith(("mailto:", "https://")):
        raise ConfigError("[SECRETS] vapid_subject must be a mailto: or https:// URL, e.g. mailto:admin@example.org")
    config.ingest_secret = read_secret(Path(config.ingest_secret_file), "[SECRETS] ingest_secret_file")
    return config

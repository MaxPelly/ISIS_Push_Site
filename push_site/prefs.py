"""Per-device notification preferences: topics and quiet hours."""
from datetime import datetime, time as dtime
from typing import Optional
from zoneinfo import ZoneInfo

# Topics the monitor always sends; instruments add their own names.
BASE_TOPICS = ("TS1", "TS2", "Muons", "MCR", "Summary")
DEFAULT_PREFS = {"topics": list(BASE_TOPICS), "quiet": None}
MAX_TOPICS = 200
URGENT_SEVERITIES = ("warning", "attention")  # still sent in quiet hours set to warnings_only


def _hhmm(value: object) -> dtime:
    if not (isinstance(value, str) and len(value) == 5 and value[2] == ":"):
        raise ValueError("times must be HH:MM")
    return dtime(int(value[:2]), int(value[3:]))


def parse_prefs(data: object) -> dict:
    """Validated preferences from a client; raises ValueError if they don't fit.

    {"topics": [names], "quiet": null or {"start": "HH:MM", "end": "HH:MM",
     "timezone": IANA name, "warnings_only": bool}}
    """
    if not isinstance(data, dict):
        raise ValueError("expected an object")
    topics = data.get("topics")
    if not (isinstance(topics, list) and len(topics) <= MAX_TOPICS
            and all(isinstance(t, str) and 0 < len(t) <= 64 for t in topics)):
        raise ValueError("bad topics")
    quiet = data.get("quiet")
    if quiet is not None:
        if not isinstance(quiet, dict) or not isinstance(quiet.get("warnings_only"), bool):
            raise ValueError("bad quiet hours")
        if _hhmm(quiet.get("start")) == _hhmm(quiet.get("end")):
            raise ValueError("quiet hours must start and end at different times")
        try:
            ZoneInfo(quiet.get("timezone"))
        except (TypeError, ValueError, KeyError, OSError):  # ZoneInfoNotFoundError is a KeyError
            raise ValueError("unknown timezone")
        quiet = {key: quiet[key] for key in ("start", "end", "timezone", "warnings_only")}
    return {"topics": sorted(set(topics)), "quiet": quiet}


def wants(prefs: dict, notification: dict, now: Optional[datetime] = None) -> bool:
    """Whether a device with these preferences should be pushed this notification now."""
    if notification["topic"] not in prefs["topics"]:
        return False
    quiet = prefs["quiet"]
    if not quiet or (quiet["warnings_only"] and notification["severity"] in URGENT_SEVERITIES):
        return True
    tz = ZoneInfo(quiet["timezone"])
    local = (now or datetime.now(tz)).astimezone(tz).time()
    start, end = _hhmm(quiet["start"]), _hhmm(quiet["end"])
    in_quiet = start <= local < end if start < end else (local >= start or local < end)  # e.g. 22:00-07:00
    return not in_quiet

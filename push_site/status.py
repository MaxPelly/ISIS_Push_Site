"""The monitor's status: its latest view of the beams, instruments and MCR news.

The monitor POSTs it, signed like a notification, to /ingest/status every
minute or so and on significant changes; the site keeps the latest and serves
it to signed-in devices. Version 1:

    {"v": 1, "kind": "status", "generated_at": ISO time, "interval_s": seconds between sends,
     "monitor_version": str, "health": {component: status},
     "beams": [{"name", "current": µA or null, "power", "since": ISO time or null,
                "history": {"interval_s", "values": [[µA or null, power], ...]}}],
     "instruments": [{"name", "beam", "run_number", "run_state", "title",
                      "started_at": ISO time or null, "counts": µA·h or null,
                      "target": µA·h or null, "auto": bool, "rb"}],
     "mcr": {"news", "seen_at": ISO time or null}}

parse_status() keeps exactly these fields, type-checked and bounded, so
nothing else reaches the page. A status from the monitor's 20 or so
instruments, at the longest strings, is well under the site's 64 KB request
limit; the list bounds stop anything else growing without limit.
"""
import json
import math
from datetime import datetime
from typing import Any, Callable

MAX_BEAMS, MAX_HISTORY, MAX_INSTRUMENTS, MAX_HEALTH = 3, 200, 64, 8
SHORT, LABEL, TITLE, NEWS = 16, 32, 200, 1000  # string lengths


def _str(limit: int) -> Callable[[Any], str]:
    def check(value: Any) -> str:
        if not isinstance(value, str) or len(value) > limit:
            raise ValueError(f"expected text of at most {limit} characters")
        return value
    return check


def _number(value: Any) -> float:
    try:
        if isinstance(value, bool) or not isinstance(value, (int, float)) or not math.isfinite(value):
            raise ValueError
        return float(value)
    except (ValueError, OverflowError):  # e.g. a 400-digit integer
        raise ValueError("expected a finite number") from None


def _time(value: Any) -> str:
    if not isinstance(value, str) or len(value) > LABEL or datetime.fromisoformat(value).tzinfo is None:
        raise ValueError("expected an ISO time with a zone")
    return value


def _bool(value: Any) -> bool:
    if not isinstance(value, bool):
        raise ValueError("expected true or false")
    return value


def _fail(message: str):
    raise ValueError(message)


def _optional(check: Callable) -> Callable:
    return lambda value: None if value is None else check(value)


def _list(check: Callable, limit: int) -> Callable[[Any], list]:
    def checked(value: Any) -> list:
        if not isinstance(value, list) or len(value) > limit:
            raise ValueError(f"expected a list of at most {limit}")
        return [check(item) for item in value]
    return checked


def _object(**fields: Callable) -> Callable[[Any], dict]:
    def checked(value: Any) -> dict:
        if not isinstance(value, dict):
            raise ValueError("expected an object")
        result = {}
        for key, check in fields.items():
            if key not in value:
                raise ValueError(f"{key} is missing")
            try:
                result[key] = check(value[key])
            except ValueError as exc:
                raise ValueError(f"{key}: {exc}") from None
        return result
    return checked


def _sample(value: Any) -> list:
    if not isinstance(value, list) or len(value) != 2:
        raise ValueError("expected [current, power]")
    return [_optional(_number)(value[0]), _str(SHORT)(value[1])]


def _health(value: Any) -> dict:
    if not isinstance(value, dict) or len(value) > MAX_HEALTH:
        raise ValueError(f"expected an object of at most {MAX_HEALTH}")
    return {_str(SHORT)(k): _str(SHORT)(v) for k, v in value.items()}


def _interval(value: Any) -> float:
    if not 1 <= _number(value) <= 86400:
        raise ValueError("expected 1 to 86400 seconds")
    return float(value)


_STATUS = _object(
    v=lambda v: v if type(v) is int and v == 1 else _fail("expected version 1"),
    kind=lambda v: v if v == "status" else _fail('expected "status"'),
    generated_at=_time,
    interval_s=_interval,
    monitor_version=_str(LABEL),
    health=_health,
    beams=_list(_object(
        name=_str(SHORT), current=_optional(_number), power=_str(SHORT), since=_optional(_time),
        history=_object(interval_s=_interval, values=_list(_sample, MAX_HISTORY)),
    ), MAX_BEAMS),
    instruments=_list(_object(
        name=_str(LABEL), beam=_str(SHORT), run_number=_str(LABEL), run_state=_str(LABEL), title=_str(TITLE),
        started_at=_optional(_time), counts=_optional(_number), target=_optional(_number), auto=_bool,
        rb=_str(LABEL),
    ), MAX_INSTRUMENTS),
    mcr=_object(news=_str(NEWS), seen_at=_optional(_time)),
)


def parse_status(data: Any) -> dict:
    """The status, with only the fields above; raises ValueError if it doesn't fit."""
    status = _STATUS(data)
    json.dumps(status, ensure_ascii=False).encode()  # UnicodeEncodeError (a ValueError) on lone surrogates
    return status

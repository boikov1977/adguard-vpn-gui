"""History of real endpoint measurements per location (T-40).

The CLI writes one ``Using endpoint`` line to ``tunnel.log`` per connection.
``tunnel.EndpointCache`` exposes only the *current* tunnel; this module keeps
the last known measurement for locations the user has actually visited, so a
switching user still sees an honest number for the previous server.

The cache is local-only (``~/.cache/adguard-vpn-gui/endpoints.json``) and is
never mixed with AdGuard's estimate: the UI labels history with its date and
shows the estimate (``≈``) when there is no fresh measurement.
"""
from __future__ import annotations

import json
import re
import threading
from dataclasses import dataclass
from datetime import datetime, timedelta
from pathlib import Path

from . import tunnel, util

log = util.get_logger("pings")

CACHE_FILE = util.CACHE_DIR / "endpoints.json"
MAX_AGE = timedelta(days=7)
PRUNE_AGE = timedelta(days=90)
VERSION = 1


def location_key(iso: str | None, city: str | None) -> str | None:
    """Stable cache key: ``ISO/City`` (both normalized), or ``None``.

    The key is intentionally case-insensitive: the CLI spells the same city
    differently across status lines ("Helsinki", "HELSINKI").  An empty ISO is
    rejected: without it the same city name could belong to another country,
    and the cache would attribute a measurement to the wrong row.
    """
    city_norm = re.sub(r"\s+", " ", (city or "").strip()).casefold()
    iso_norm = (iso or "").strip().upper()
    if not city_norm or not iso_norm:
        return None
    return f"{iso_norm}/{city_norm}"


def short_date(value: datetime | None) -> str:
    return value.strftime("%d.%m") if value else ""


def history_text(ping: int | None, measured_at: datetime | None) -> str:
    """Compact history value, e.g. ``231 мс · 22.11``."""
    if ping is None or ping < 0 or ping >= 9999:
        return "—"
    date = short_date(measured_at)
    return f"{util.human_ping(ping)} · {date}" if date else util.human_ping(ping)


def is_fresh(measured_at: datetime | None, now: datetime | None = None,
             max_age: timedelta = MAX_AGE) -> bool:
    if measured_at is None:
        return False
    now = now or datetime.now()
    return now - measured_at <= max_age


@dataclass(frozen=True)
class Measurement:
    """One real endpoint measurement stored for a location."""

    endpoint: str
    address: str
    ping: int
    measured_at: datetime


def _measurement_from_dict(value) -> Measurement | None:
    if not isinstance(value, dict):
        return None
    try:
        ping = int(value.get("ping"))
    except (TypeError, ValueError):
        return None
    if ping < 0 or ping >= 9999:
        return None
    try:
        measured_at = datetime.fromisoformat(str(value.get("measured_at")))
    except (TypeError, ValueError):
        return None
    return Measurement(
        endpoint=str(value.get("endpoint") or ""),
        address=str(value.get("address") or ""),
        ping=ping,
        measured_at=measured_at,
    )


def _measurement_to_dict(measurement: Measurement) -> dict:
    return {
        "endpoint": measurement.endpoint,
        "address": measurement.address,
        "ping": measurement.ping,
        "measured_at": measurement.measured_at.isoformat(timespec="seconds"),
    }


class EndpointHistory:
    """Persistent, thread-safe history of real measurements.

    Corrupt or unreadable cache files are treated as empty: a broken cache must
    never prevent the UI from starting.
    """

    def __init__(self, path: str | Path | None = None,
                 max_age: timedelta = MAX_AGE) -> None:
        self.path = Path(path) if path is not None else CACHE_FILE
        self.max_age = max_age
        self._lock = threading.RLock()
        self._items = self._load()

    def _load(self) -> dict[str, Measurement]:
        try:
            raw = json.loads(self.path.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return {}
        except Exception:
            log.warning("Could not read endpoint history", exc_info=True)
            return {}
        locations = raw.get("locations") if isinstance(raw, dict) else None
        if not isinstance(locations, dict):
            return {}
        items: dict[str, Measurement] = {}
        for key, value in locations.items():
            measurement = _measurement_from_dict(value)
            if measurement is not None:
                items[str(key)] = measurement
        return items

    def _save(self, now: datetime) -> None:
        cutoff = now - PRUNE_AGE
        items = {
            key: value for key, value in self._items.items()
            if value.measured_at >= cutoff
        }
        self._items = items
        payload = {
            "version": VERSION,
            "locations": {
                key: _measurement_to_dict(value) for key, value in items.items()
            },
        }
        try:
            util.atomic_write_text(self.path, json.dumps(payload, ensure_ascii=False, indent=2))
        except Exception:
            log.warning("Could not save endpoint history", exc_info=True)

    def record(self, loc, endpoint: tunnel.Endpoint | None,
               now: datetime | None = None) -> bool:
        """Store a fresh measurement for ``loc``; return ``True`` if changed."""
        if loc is None or endpoint is None or endpoint.ping is None:
            return False
        key = location_key(getattr(loc, "iso", ""), getattr(loc, "city", ""))
        if key is None:
            return False
        now = now or datetime.now()
        try:
            ping = int(endpoint.ping)
        except (TypeError, ValueError):
            return False
        with self._lock:
            previous = self._items.get(key)
            measured_at = endpoint.measured_at
            if measured_at is None:
                # Without a log timestamp, keep the first record time instead
                # of rewriting the file on every poll.
                if (previous is not None and previous.ping == ping
                        and previous.endpoint == (endpoint.name or "")
                        and previous.address == (endpoint.address or "")):
                    return False
                measured_at = now
            measurement = Measurement(
                endpoint=endpoint.name or "",
                address=endpoint.address or "",
                ping=ping,
                measured_at=measured_at.replace(microsecond=0),
            )
            if previous == measurement:
                return False
            self._items[key] = measurement
            self._save(now)
            return True

    def get(self, loc, now: datetime | None = None) -> Measurement | None:
        """Return a measurement no older than ``max_age``, else ``None``."""
        key = location_key(getattr(loc, "iso", ""), getattr(loc, "city", ""))
        if key is None:
            return None
        with self._lock:
            measurement = self._items.get(key)
        if measurement is None:
            return None
        if not is_fresh(measurement.measured_at, now, self.max_age):
            return None
        return measurement

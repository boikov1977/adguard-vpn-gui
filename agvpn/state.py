"""Persistent local state (preferences, last location, activity)."""
from __future__ import annotations

import json
import math
import re
import threading
import time
from dataclasses import dataclass, field
from typing import Any

from . import util

# Favourite and recently used locations are stored as flat ``ISO/city`` keys.
# The city part is casefolded so matching never depends on the CLI casing.
MAX_RECENT = 5
MAX_FAVORITES = 100

DEFAULT_PREFS: dict[str, Any] = {
    "minimize_to_tray": True,
    "show_notifications": True,
    "autostart": False,
    "connect_on_start": False,
}

DEFAULT_WINDOW: dict[str, Any] = {
    "width": 1120,
    "height": 750,
    "x": None,
    "y": None,
    "maximized": False,
}

DEFAULT_STATE: dict[str, Any] = {
    "last_location": None,
    "connected_since": None,
    "activity": [],
    "window": dict(DEFAULT_WINDOW),
    "prefs": dict(DEFAULT_PREFS),
    "first_close_hint_shown": False,
    "favorites": [],
    "recent": [],
}

# Sanity bounds for values read from a hand-edited / legacy state file.
_WINDOW_MAX_DIMENSION = 100_000
_WINDOW_MAX_COORD = 100_000
_ISO_RE = re.compile(r"^[A-Z]{2,3}$")


def _clean_text(value: Any) -> str:
    """Collapse whitespace in a user/CLI supplied string."""
    return " ".join(str(value).split())


def _timestamp(value: Any) -> float | None:
    """Return a sane Unix timestamp, or ``None`` for a corrupt value."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (TypeError, ValueError, OverflowError):
        return None
    if not math.isfinite(number) or number <= 0:
        return None
    return number


def location_key(iso: Any, city: Any) -> str | None:
    """Build a stable ``ISO/city`` key, or ``None`` when it is unusable.

    ISO is uppercased, the city is casefolded, and repeated whitespace is
    collapsed on both parts.  Empty or malformed values are ignored, so a
    synthetic status-only location never ends up in the favourites list.
    """
    if not isinstance(iso, str) or not isinstance(city, str):
        return None
    iso_norm = _clean_text(iso).upper()
    city_norm = _clean_text(city).casefold()
    if not iso_norm or not city_norm:
        return None
    if not _ISO_RE.fullmatch(iso_norm):
        return None
    if "/" in city_norm:
        return None
    return f"{iso_norm}/{city_norm}"


def _sanitise_keys(raw: Any, limit: int) -> list[str]:
    """Return a clean, de-duplicated list of location keys."""
    if not isinstance(raw, list) or limit <= 0:
        return []
    result: list[str] = []
    seen: set[str] = set()
    for item in raw:
        if not isinstance(item, str):
            continue
        iso, sep, city = item.partition("/")
        if not sep:
            continue
        key = location_key(iso, city)
        if key is None or key in seen:
            continue
        seen.add(key)
        result.append(key)
        if len(result) >= limit:
            break
    return result


def _window_dimension(value: Any, default: int) -> int:
    """Return a positive pixel dimension, falling back to *default*."""
    if isinstance(value, bool):
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    if number <= 0:
        return default
    return min(number, _WINDOW_MAX_DIMENSION)


def _window_coord(value: Any) -> int | None:
    """Return a sane screen coordinate or ``None`` when it is unusable."""
    if value is None or isinstance(value, bool):
        return None
    try:
        number = int(value)
    except (TypeError, ValueError):
        return None
    if abs(number) > _WINDOW_MAX_COORD:
        return None
    return number


def _normalise_window(raw: Any) -> dict[str, Any]:
    win = raw if isinstance(raw, dict) else {}
    width = _window_dimension(win.get("width"), DEFAULT_WINDOW["width"])
    height = _window_dimension(win.get("height"), DEFAULT_WINDOW["height"])
    x = _window_coord(win.get("x"))
    y = _window_coord(win.get("y"))
    # A position is only meaningful as a pair; restore logic centres the
    # window when either coordinate is missing.
    if x is None or y is None:
        x = y = None
    return {
        "width": width,
        "height": height,
        "x": x,
        "y": y,
        "maximized": bool(win.get("maximized", False)),
    }


class StateStore:
    def __init__(self) -> None:
        self.path = util.CONFIG_DIR / "state.json"
        self._lock = threading.RLock()
        self._data = self._load()

    def _load(self) -> dict[str, Any]:
        data = json.loads(json.dumps(DEFAULT_STATE))
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            if isinstance(raw, dict):
                for key, value in raw.items():
                    if key in ("prefs", "window") and isinstance(value, dict):
                        data.setdefault(key, {}).update(value)
                    else:
                        data[key] = value
        except FileNotFoundError:
            pass
        except Exception:
            util.get_logger("state").warning("Could not read state file", exc_info=True)
        if not isinstance(data.get("activity"), list):
            data["activity"] = []
        data["activity"] = data["activity"][-40:]
        prefs = data.setdefault("prefs", {})
        for key, value in DEFAULT_PREFS.items():
            prefs.setdefault(key, value)
        data["favorites"] = _sanitise_keys(data.get("favorites"), MAX_FAVORITES)
        data["recent"] = _sanitise_keys(data.get("recent"), MAX_RECENT)
        data["connected_since"] = _timestamp(data.get("connected_since"))
        return data

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def save(self) -> None:
        with self._lock:
            try:
                util.atomic_write_text(
                    self.path,
                    json.dumps(self._data, ensure_ascii=False, indent=2),
                )
            except Exception:
                util.get_logger("state").warning("Could not save state file", exc_info=True)

    # -- convenient accessors -------------------------------------------------
    def pref(self, key: str, default=None):
        return self._data.get("prefs", {}).get(key, DEFAULT_PREFS.get(key, default))

    def set_pref(self, key: str, value) -> None:
        self._data.setdefault("prefs", {})[key] = value
        self.save()

    @property
    def last_location(self) -> dict | None:
        value = self._data.get("last_location")
        return value if isinstance(value, dict) else None

    def set_last_location(self, iso: str, country: str, city: str, ping: int | None) -> None:
        self._data["last_location"] = {
            "iso": iso, "country": country, "city": city, "ping": ping,
        }
        self.save()

    @property
    def connected_since(self) -> float | None:
        return _timestamp(self._data.get("connected_since"))

    def set_connected_since(self, ts: float | None) -> None:
        self._data["connected_since"] = ts
        self.save()

    def add_activity(self, kind: str, title: str, subtitle: str = "") -> None:
        self._data.setdefault("activity", []).append({
            "ts": time.time(), "kind": kind, "title": title, "subtitle": subtitle,
        })
        self._data["activity"] = self._data["activity"][-40:]
        self.save()

    @property
    def activity(self) -> list[dict]:
        return list(self._data.get("activity", []))

    # -- favourites / recents -------------------------------------------------
    @property
    def favorite_keys(self) -> list[str]:
        with self._lock:
            return list(self._data.get("favorites", []))

    @property
    def recent_keys(self) -> list[str]:
        with self._lock:
            return list(self._data.get("recent", []))

    def is_favorite(self, iso: str, city: str) -> bool:
        key = location_key(iso, city)
        if key is None:
            return False
        with self._lock:
            return key in self._data.get("favorites", [])

    def toggle_favorite(self, iso: str, city: str) -> bool:
        """Toggle a location and return its new favourite state."""
        key = location_key(iso, city)
        if key is None:
            return False
        with self._lock:
            favorites = _sanitise_keys(self._data.get("favorites"), MAX_FAVORITES)
            if key in favorites:
                self._data["favorites"] = [item for item in favorites if item != key]
                active = False
            else:
                self._data["favorites"] = ([key] + favorites)[:MAX_FAVORITES]
                active = True
            self.save()
        return active

    def remember_recent(self, iso: str, city: str) -> None:
        """Move a location to the front of the recent list (max 5)."""
        key = location_key(iso, city)
        if key is None:
            return
        with self._lock:
            recent = _sanitise_keys(self._data.get("recent"), MAX_RECENT)
            recent = [item for item in recent if item != key]
            self._data["recent"] = ([key] + recent)[:MAX_RECENT]
            self.save()

    @property
    def window_geometry(self) -> dict[str, int | bool | None]:
        """Saved window size/position with safe fallbacks.

        Old state files only contain ``width``/``height``; the missing position
        and ``maximized`` flag fall back to the defaults (centred, not
        maximised).
        """
        return _normalise_window(self._data.get("window"))

    def set_window_geometry(self, width: int, height: int, x: int | None = None,
                            y: int | None = None, maximized: bool = False) -> None:
        with self._lock:
            self._data["window"] = _normalise_window({
                "width": width,
                "height": height,
                "x": x,
                "y": y,
                "maximized": maximized,
            })
            self.save()

    @property
    def window_size(self) -> tuple[int, int]:
        geometry = self.window_geometry
        return int(geometry["width"]), int(geometry["height"])

    def set_window_size(self, width: int, height: int) -> None:
        """Update only the size, keeping the saved position and state."""
        geometry = self.window_geometry
        self.set_window_geometry(
            width, height, geometry["x"], geometry["y"], bool(geometry["maximized"])
        )


@dataclass
class LocationCache:
    items: list[dict] = field(default_factory=list)
    ts: float = 0.0

    @property
    def fresh(self) -> bool:
        return bool(self.items) and (time.time() - self.ts) < 6 * 3600


class LocationsCache:
    def __init__(self) -> None:
        self.path = util.CACHE_DIR / "locations.json"
        self._cache = self._load()

    def _load(self) -> LocationCache:
        try:
            with open(self.path, "r", encoding="utf-8") as fh:
                raw = json.load(fh)
            items = raw.get("items") or []
            if isinstance(items, list):
                clean = [
                    item for item in items
                    if isinstance(item, dict) and item.get("iso") and item.get("city")
                ]
                return LocationCache(items=clean, ts=float(raw.get("ts") or 0))
        except FileNotFoundError:
            pass
        except Exception:
            util.get_logger("state").warning("Could not read location cache", exc_info=True)
        return LocationCache()

    @property
    def items(self) -> list[dict]:
        return list(self._cache.items)

    @property
    def fresh(self) -> bool:
        return self._cache.fresh

    def save(self, items: list[dict]) -> None:
        self._cache = LocationCache(items=items, ts=time.time())
        try:
            util.atomic_write_text(self.path, json.dumps(
                {"ts": self._cache.ts, "items": items}, ensure_ascii=False
            ))
        except Exception:
            util.get_logger("state").warning("Could not save location cache", exc_info=True)

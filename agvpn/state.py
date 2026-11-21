"""Persistent local state (preferences, last location, activity)."""
from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from . import util

DEFAULT_PREFS: dict[str, Any] = {
    "minimize_to_tray": True,
    "show_notifications": True,
    "autostart": False,
}

DEFAULT_STATE: dict[str, Any] = {
    "last_location": None,
    "connected_since": None,
    "activity": [],
    "window": {"width": 1120, "height": 750},
    "prefs": dict(DEFAULT_PREFS),
    "first_close_hint_shown": False,
}


def _atomic_write(path: Path, data: str) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    fh = tempfile.NamedTemporaryFile(
        "w", encoding="utf-8", dir=str(path.parent), prefix=path.name + ".", delete=False
    )
    try:
        fh.write(data)
        fh.flush()
        os.fsync(fh.fileno())
        os.chmod(fh.name, 0o600)
        fh.close()
        os.replace(fh.name, path)
    finally:
        try:
            if not fh.closed:
                fh.close()
        except Exception:
            pass


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
        return data

    @property
    def data(self) -> dict[str, Any]:
        return self._data

    def save(self) -> None:
        with self._lock:
            try:
                _atomic_write(self.path, json.dumps(self._data, ensure_ascii=False, indent=2))
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
        value = self._data.get("connected_since")
        return float(value) if value else None

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

    @property
    def window_size(self) -> tuple[int, int]:
        win = self._data.get("window") or {}
        try:
            return int(win.get("width", 1120)), int(win.get("height", 750))
        except Exception:
            return 1120, 750

    def set_window_size(self, width: int, height: int) -> None:
        self._data["window"] = {"width": int(width), "height": int(height)}
        self.save()


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
            _atomic_write(self.path, json.dumps(
                {"ts": self._cache.ts, "items": items}, ensure_ascii=False
            ))
        except Exception:
            util.get_logger("state").warning("Could not save location cache", exc_info=True)

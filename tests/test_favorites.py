#!/usr/bin/python3
"""Favourite / recent locations tests.  Run:  python3 -m unittest discover -s tests -v

The store tests use a temporary state file and the ordering helper is pure, so
no display or GTK widgets are required.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import state as state_mod  # noqa: E402
from agvpn.app import order_locations  # noqa: E402
from agvpn.cli import Location  # noqa: E402
from agvpn.state import MAX_RECENT, StateStore, location_key  # noqa: E402


def loc(iso: str, city: str, ping: int | None = 20) -> Location:
    return Location(iso=iso, country="", city=city, ping=ping)


class _StoreMixin:
    def _store(self, raw: dict | None = None, directory: Path | None = None) -> StateStore:
        if directory is None:
            tmp = tempfile.TemporaryDirectory()
            self.addCleanup(tmp.cleanup)
            directory = Path(tmp.name)
        if raw is not None:
            (directory / "state.json").write_text(json.dumps(raw), encoding="utf-8")
        with mock.patch.object(state_mod.util, "CONFIG_DIR", directory):
            return StateStore()


class LocationKeyTests(unittest.TestCase):
    def test_iso_and_city_are_normalised(self):
        self.assertEqual(location_key(" fi ", "  Helsinki  "), "FI/helsinki")
        self.assertEqual(location_key("us", "New   York"), "US/new york")

    def test_empty_parts_are_rejected(self):
        self.assertIsNone(location_key("", "Helsinki"))
        self.assertIsNone(location_key("FI", "   "))

    def test_malformed_iso_is_rejected(self):
        self.assertIsNone(location_key("F1", "Helsinki"))
        self.assertIsNone(location_key("FI/SE", "Helsinki"))

    def test_non_strings_are_rejected(self):
        self.assertIsNone(location_key(None, "Helsinki"))
        self.assertIsNone(location_key("FI", 42))


class FavouriteStoreTests(_StoreMixin, unittest.TestCase):
    def test_defaults_are_empty(self):
        store = self._store()
        self.assertEqual(store.favorite_keys, [])
        self.assertEqual(store.recent_keys, [])

    def test_toggle_adds_and_removes(self):
        store = self._store()
        self.assertTrue(store.toggle_favorite("FI", "Helsinki"))
        self.assertTrue(store.is_favorite("fi", "HELSINKI"))
        self.assertEqual(store.favorite_keys, ["FI/helsinki"])
        self.assertFalse(store.toggle_favorite("FI", "Helsinki"))
        self.assertFalse(store.is_favorite("FI", "Helsinki"))
        self.assertEqual(store.favorite_keys, [])

    def test_toggle_is_case_and_space_insensitive(self):
        store = self._store()
        store.toggle_favorite("FI", "New  York")
        self.assertTrue(store.is_favorite("fi", " new york "))
        self.assertFalse(store.toggle_favorite("FI", "NEW YORK"))
        self.assertEqual(store.favorite_keys, [])

    def test_invalid_location_is_ignored(self):
        store = self._store()
        self.assertFalse(store.toggle_favorite("", "Helsinki"))
        self.assertFalse(store.toggle_favorite("FI", ""))
        self.assertFalse(store.is_favorite("", "Helsinki"))
        self.assertEqual(store.favorite_keys, [])

    def test_new_favourite_goes_first(self):
        store = self._store()
        store.toggle_favorite("FI", "Helsinki")
        store.toggle_favorite("SE", "Stockholm")
        self.assertEqual(store.favorite_keys, ["SE/stockholm", "FI/helsinki"])

    def test_keys_are_copies(self):
        store = self._store()
        store.toggle_favorite("FI", "Helsinki")
        keys = store.favorite_keys
        keys.append("XX/nowhere")
        self.assertEqual(store.favorite_keys, ["FI/helsinki"])

    def test_persisted_and_reloaded(self):
        store = self._store()
        store.toggle_favorite("FI", "Helsinki")
        store.remember_recent("US", "New York")
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["favorites"], ["FI/helsinki"])
        self.assertEqual(raw["recent"], ["US/new york"])
        reloaded = self._store(raw)
        self.assertTrue(reloaded.is_favorite("FI", "Helsinki"))
        self.assertEqual(reloaded.recent_keys, ["US/new york"])


class RecentStoreTests(_StoreMixin, unittest.TestCase):
    def test_remember_deduplicates_and_moves_to_front(self):
        store = self._store()
        store.remember_recent("FI", "Helsinki")
        store.remember_recent("SE", "Stockholm")
        store.remember_recent("fi", "HELSINKI")
        self.assertEqual(store.recent_keys, ["FI/helsinki", "SE/stockholm"])

    def test_recent_is_limited(self):
        store = self._store()
        for index in range(8):
            store.remember_recent("FI", f"City {index}")
        self.assertEqual(len(store.recent_keys), MAX_RECENT)
        self.assertEqual(
            store.recent_keys,
            ["FI/city 7", "FI/city 6", "FI/city 5", "FI/city 4", "FI/city 3"],
        )

    def test_old_state_without_favourites_is_valid(self):
        store = self._store({"last_location": {"iso": "FI", "city": "Helsinki"}})
        self.assertEqual(store.favorite_keys, [])
        self.assertEqual(store.recent_keys, [])
        self.assertTrue(store.toggle_favorite("FI", "Helsinki"))

    def test_corrupted_lists_are_sanitised(self):
        store = self._store({
            "favorites": [
                "FI/Helsinki",
                " fi /  HELSINKI ",
                "fi/espoo",
                42,
                "broken",
                "FI/",
                None,
            ],
            "recent": "not-a-list",
        })
        self.assertEqual(store.favorite_keys, ["FI/helsinki", "FI/espoo"])
        self.assertEqual(store.recent_keys, [])

    def test_recent_limit_is_applied_when_loading(self):
        raw = {"recent": [f"FI/city {index}" for index in range(12)]}
        store = self._store(raw)
        self.assertEqual(len(store.recent_keys), MAX_RECENT)
        self.assertEqual(store.recent_keys[0], "FI/city 0")

    def test_favourites_limit_is_applied_when_loading(self):
        raw = {"favorites": [f"FI/city {index}" for index in range(150)]}
        store = self._store(raw)
        self.assertEqual(len(store.favorite_keys), state_mod.MAX_FAVORITES)


class OrderLocationsTests(unittest.TestCase):
    def setUp(self):
        self.helsinki = loc("FI", "Helsinki", 22)
        self.espoo = loc("FI", "Espoo", 24)
        self.york = loc("GB", "York", 30)
        self.new_york = loc("US", "New York", 90)
        self.items = [self.helsinki, self.espoo, self.york, self.new_york]

    def test_favourites_come_first_in_favourite_order(self):
        ordered = order_locations(self.items, ["GB/york", "US/new york"])
        self.assertEqual(ordered[:2], [self.york, self.new_york])
        self.assertEqual(ordered[2:], [self.helsinki, self.espoo])

    def test_rest_keeps_current_order(self):
        ordered = order_locations(self.items, ["US/new york"])
        self.assertEqual(ordered[1:], [self.helsinki, self.espoo, self.york])

    def test_limit_is_applied_after_ordering(self):
        ordered = order_locations(self.items, ["US/new york"], 2)
        self.assertEqual(ordered, [self.new_york, self.helsinki])

    def test_zero_or_negative_limit_returns_empty(self):
        self.assertEqual(order_locations(self.items, [], 0), [])
        self.assertEqual(order_locations(self.items, [], -1), [])

    def test_locations_without_iso_are_not_promoted(self):
        synthetic = loc("", "Amsterdam", None)
        ordered = order_locations([synthetic, self.helsinki], ["FI/helsinki"])
        self.assertEqual(ordered, [self.helsinki, synthetic])


if __name__ == "__main__":
    unittest.main()

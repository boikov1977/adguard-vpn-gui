#!/usr/bin/python3
"""Corrupted ``state.json`` must never break startup (review #7).

Only temporary files are touched; no display and no network are needed.
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
from agvpn.state import DEFAULT_PREFS, StateStore  # noqa: E402


class CorruptedStateTests(unittest.TestCase):
    def _store(self, content) -> StateStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        if content is not None:
            path = directory / "state.json"
            if isinstance(content, bytes):
                path.write_bytes(content)
            elif isinstance(content, str):
                path.write_text(content, encoding="utf-8")
            else:
                path.write_text(json.dumps(content), encoding="utf-8")
        with mock.patch.object(state_mod.util, "CONFIG_DIR", directory):
            return StateStore()

    def test_prefs_list_falls_back_to_defaults(self):
        store = self._store({"prefs": []})
        self.assertEqual(store.data["prefs"], DEFAULT_PREFS)
        self.assertFalse(store.pref("autostart"))

    def test_prefs_with_wrong_value_type_use_defaults(self):
        store = self._store({"prefs": {"autostart": "yes", "connect_on_start": True}})
        self.assertFalse(store.pref("autostart"))
        self.assertTrue(store.pref("connect_on_start"))

    def test_overflowing_dimension_falls_back(self):
        store = self._store('{"window": {"width": 1e309, "height": 1e309}}')
        geometry = store.window_geometry
        self.assertEqual(geometry["width"], 1120)
        self.assertEqual(geometry["height"], 750)

    def test_nan_dimension_falls_back(self):
        store = self._store('{"window": {"width": NaN, "height": Infinity}}')
        geometry = store.window_geometry
        self.assertEqual(geometry["width"], 1120)
        self.assertEqual(geometry["height"], 750)

    def test_activity_with_non_dict_entries_is_filtered(self):
        store = self._store({"activity": [None, "oops", 7, {"ts": "bad"}]})
        self.assertEqual(store.activity, [])
        geometry = store.window_geometry
        self.assertIn("width", geometry)

    def test_activity_keeps_only_well_formed_entries(self):
        store = self._store({"activity": [
            None,
            {"ts": 1700000000, "kind": "connect", "title": "Подключено",
             "subtitle": "Helsinki"},
        ]})
        items = store.activity
        self.assertEqual(len(items), 1)
        self.assertEqual(items[0]["kind"], "connect")
        self.assertEqual(items[0]["title"], "Подключено")
        for item in items:
            self.assertIsInstance(item, dict)
            self.assertIsInstance(item.get("ts"), float)

    def test_garbage_file_uses_defaults(self):
        for content in ("not json", "null", "[1, 2, 3]", '"string"', b"\xff\xfe\x00"):
            with self.subTest(content=content):
                store = self._store(content)
                self.assertEqual(store.data["prefs"], DEFAULT_PREFS)
                self.assertEqual(store.window_geometry["width"], 1120)
                self.assertEqual(store.activity, [])
                self.assertIsNone(store.last_location)

    def test_last_location_of_wrong_type_is_dropped(self):
        store = self._store({"last_location": ["FI", "Helsinki"]})
        self.assertIsNone(store.last_location)

    def test_last_location_with_non_string_city_is_safe(self):
        store = self._store({"last_location": {
            "iso": "FI", "country": "Finland", "city": ["Helsinki"], "ping": 17,
        }})
        last = store.last_location
        self.assertIsNotNone(last)
        self.assertIsNone(last["city"])
        # The reader used by the connected-status handler must not raise.
        self.assertEqual((last.get("city") or "").lower(), "")
        self.assertEqual(last["iso"], "FI")
        self.assertEqual(last["ping"], 17)

    def test_last_location_with_bad_ping_falls_back(self):
        for ping in ("abc", True, [17], {"v": 17}):
            with self.subTest(ping=ping):
                store = self._store({"last_location": {
                    "iso": "FI", "city": "Helsinki", "ping": ping,
                }})
                self.assertIsNone(store.last_location["ping"])
                self.assertEqual(store.last_location["city"], "Helsinki")

    def test_last_location_drops_unknown_fields(self):
        store = self._store({"last_location": {
            "iso": "FI", "country": "Finland", "city": " Helsinki ",
            "ping": 17.9, "junk": {"a": 1},
        }})
        self.assertEqual(store.last_location, {
            "iso": "FI", "country": "Finland", "city": "Helsinki", "ping": 17,
        })

    def test_first_close_hint_is_coerced(self):
        store = self._store({"first_close_hint_shown": "yes"})
        self.assertTrue(store.data["first_close_hint_shown"])


if __name__ == "__main__":
    unittest.main()

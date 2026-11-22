#!/usr/bin/python3
"""Window geometry tests.  Run:  python3 -m unittest discover -s tests -v

The clamping helper is pure and the store tests use a temporary state file, so
no display or GTK widgets are required.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import state as state_mod  # noqa: E402
from agvpn.app import MainWindow, fit_window_geometry  # noqa: E402
from agvpn.state import DEFAULT_PREFS, StateStore  # noqa: E402


def workarea(x: int = 0, y: int = 0, width: int = 1920, height: int = 1080):
    return SimpleNamespace(x=x, y=y, width=width, height=height)


class FitWindowGeometryTests(unittest.TestCase):
    def test_negative_position_is_clamped_to_primary_monitor(self):
        fitted = fit_window_geometry(1120, 750, -500, -200, workarea())
        self.assertEqual(fitted["x"], 0)
        self.assertEqual(fitted["y"], 0)

    def test_negative_monitor_keeps_valid_position(self):
        # A monitor to the left of the primary has negative coordinates.
        fitted = fit_window_geometry(1120, 750, -1500, 40, workarea(-1920, 0))
        self.assertEqual((fitted["x"], fitted["y"]), (-1500, 40))

    def test_window_larger_than_workarea_is_shrunk(self):
        fitted = fit_window_geometry(3000, 2000, 0, 0, workarea())
        self.assertEqual(fitted["width"], 1920 - 56)
        self.assertEqual(fitted["height"], 1080 - 104)

    def test_position_on_second_monitor_is_preserved(self):
        fitted = fit_window_geometry(1120, 750, 2000, 100, workarea(1920, 0))
        self.assertEqual((fitted["x"], fitted["y"]), (2000, 100))

    def test_position_outside_second_monitor_is_pulled_back(self):
        fitted = fit_window_geometry(1120, 750, 9000, 5000, workarea(1920, 0))
        self.assertEqual(fitted["x"], 1920 + 1920 - 1120 - 56)
        self.assertEqual(fitted["y"], 1080 - 750 - 104)

    def test_missing_position_means_centred(self):
        fitted = fit_window_geometry(1000, 700, None, None, workarea())
        self.assertIsNone(fitted["x"])
        self.assertIsNone(fitted["y"])
        self.assertEqual((fitted["width"], fitted["height"]), (1000, 700))

    def test_partial_or_invalid_position_means_centred(self):
        for x, y in ((100, None), (None, 200), ("bad", 200)):
            fitted = fit_window_geometry(1000, 700, x, y, workarea())
            self.assertIsNone(fitted["x"], (x, y))
            self.assertIsNone(fitted["y"], (x, y))

    def test_invalid_size_falls_back_to_defaults(self):
        fitted = fit_window_geometry(-10, "oops", None, None, workarea())
        self.assertEqual((fitted["width"], fitted["height"]), (1120, 750))

    def test_no_display_keeps_saved_geometry(self):
        fitted = fit_window_geometry(1200, 800, 120, 80, None)
        self.assertEqual(fitted, {"width": 1200, "height": 800, "x": 120, "y": 80})

    def test_accepts_mapping_workarea(self):
        fitted = fit_window_geometry(
            1500, 750, -50, 20,
            {"x": 0, "y": 0, "width": 1366, "height": 768},
        )
        self.assertEqual(fitted["x"], 0)
        self.assertEqual(fitted["width"], 1310)


class FullscreenDetectionTests(unittest.TestCase):
    def test_gtk3_window_state_is_used(self):
        import gi
        gi.require_version("Gdk", "3.0")
        from gi.repository import Gdk

        class _FakeWindow:
            def __init__(self, state):
                self.state = state

            def get_state(self):
                return self.state

        class _FakeSelf:
            def __init__(self, state):
                self.window = _FakeWindow(state)

            def get_window(self):
                return self.window

        self.assertTrue(MainWindow._is_fullscreen(
            _FakeSelf(Gdk.WindowState.FULLSCREEN)
        ))
        self.assertFalse(MainWindow._is_fullscreen(_FakeSelf(0)))


class StateStoreWindowTests(unittest.TestCase):
    def _store(self, raw: dict | None = None) -> StateStore:
        tmp = tempfile.TemporaryDirectory()
        self.addCleanup(tmp.cleanup)
        directory = Path(tmp.name)
        if raw is not None:
            (directory / "state.json").write_text(json.dumps(raw), encoding="utf-8")
        with mock.patch.object(state_mod.util, "CONFIG_DIR", directory):
            return StateStore()

    def test_defaults_are_safe(self):
        geometry = self._store().window_geometry
        self.assertEqual(geometry["width"], 1120)
        self.assertEqual(geometry["height"], 750)
        self.assertIsNone(geometry["x"])
        self.assertIsNone(geometry["y"])
        self.assertFalse(geometry["maximized"])

    def test_legacy_state_without_position(self):
        store = self._store({"window": {"width": 1000, "height": 700}})
        geometry = store.window_geometry
        self.assertEqual((geometry["width"], geometry["height"]), (1000, 700))
        self.assertIsNone(geometry["x"])
        self.assertIsNone(geometry["y"])
        self.assertFalse(geometry["maximized"])
        self.assertEqual(store.window_size, (1000, 700))

    def test_corrupted_values_fall_back(self):
        store = self._store({
            "window": {"width": "oops", "height": -5, "x": True, "y": 20, "maximized": 1}
        })
        geometry = store.window_geometry
        self.assertEqual((geometry["width"], geometry["height"]), (1120, 750))
        self.assertIsNone(geometry["x"])
        self.assertIsNone(geometry["y"])
        self.assertTrue(geometry["maximized"])

    def test_geometry_is_serialised_and_reloaded(self):
        store = self._store()
        store.set_window_geometry(1280, 820, 40, 60, maximized=True)
        raw = json.loads(store.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["window"], {
            "width": 1280, "height": 820, "x": 40, "y": 60, "maximized": True,
        })
        reloaded = self._store(raw)
        self.assertEqual(reloaded.window_geometry, {
            "width": 1280, "height": 820, "x": 40, "y": 60, "maximized": True,
        })

    def test_set_window_size_keeps_position_and_maximized(self):
        store = self._store()
        store.set_window_geometry(1200, 800, 10, 20, maximized=True)
        store.set_window_size(1300, 900)
        self.assertEqual(store.window_geometry, {
            "width": 1300, "height": 900, "x": 10, "y": 20, "maximized": True,
        })

    def test_missing_pref_gets_default(self):
        store = self._store({"prefs": {"autostart": True}})
        self.assertFalse(store.pref("connect_on_start"))
        self.assertTrue(store.pref("autostart"))

    def test_connect_on_start_default_is_disabled(self):
        self.assertIs(DEFAULT_PREFS["connect_on_start"], False)

    def test_connect_on_start_pref_round_trip(self):
        store = self._store()
        store.set_pref("connect_on_start", True)
        reloaded = self._store(json.loads(store.path.read_text(encoding="utf-8")))
        self.assertTrue(reloaded.pref("connect_on_start"))


if __name__ == "__main__":
    unittest.main()

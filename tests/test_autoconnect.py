#!/usr/bin/python3
"""Startup autoconnect must wait for slow initial data, but not forever.

No display is needed: ``MainWindow`` methods run on a duck-typed stub and
``GLib.timeout_add`` is mocked, so no real timer is armed.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import app as app_mod  # noqa: E402
from agvpn.app import MainWindow  # noqa: E402
from agvpn.cli import License, Location, VpnStatus  # noqa: E402

HELSINKI = Location(iso="FI", country="Finland", city="Helsinki", ping=17)


class _FakeWindow:
    """Duck-typed stand-in for the attributes autoconnect touches."""

    _alive = True
    _shutting_down = False
    busy = False
    busy_kind = None
    _blocklist_op = False

    def __init__(self) -> None:
        self._excl_op = False
        self._auto_connect_done = False
        self._auto_connect_deadline = None
        self._auto_connect_scheduled = False
        self.status_loaded = False
        self.license_loaded = False
        self.license_error = False
        self.license = License()
        self.status = VpnStatus(state="disconnected")
        self.locations = []
        self._locations_inflight = False
        self._license_seq = 0
        self._license_inflight = False
        self.store = types.SimpleNamespace(pref=mock.Mock(return_value=True))
        self.cli = types.SimpleNamespace(available=True)
        self.connect_to = mock.Mock()
        self.current_location = mock.Mock(return_value=None)
        self._location_name = MainWindow._location_name
        self.account_page = mock.Mock()
        self._update_account_chip = mock.Mock()
        self._update_exclusion_badge = mock.Mock()
        self._run_pending_license = mock.Mock()
        for name in ("_maybe_autoconnect", "_retry_autoconnect",
                     "_schedule_autoconnect", "_autoconnect_tick",
                     "_license_done"):
            setattr(self, name, types.MethodType(getattr(MainWindow, name), self))

    @property
    def busy_or_excl(self) -> bool:
        return MainWindow.busy_or_excl.fget(self)

    def ready(self) -> None:
        self.status_loaded = True
        self.license_loaded = True
        self.license = License(logged_in=True)
        self.locations = [HELSINKI]


class AutoconnectTests(unittest.TestCase):
    def test_slow_startup_autoconnects_after_many_retries(self):
        window = _FakeWindow()
        window.locations = [HELSINKI]
        with mock.patch.object(app_mod.GLib, "timeout_add") as timeout_add:
            # Far more checks than the old ten-attempt limit.
            for _ in range(15):
                window._autoconnect_tick()
            window.connect_to.assert_not_called()
            self.assertFalse(window._auto_connect_done)

            window.ready()
            window._autoconnect_tick()

        self.assertTrue(timeout_add.called)
        window.connect_to.assert_called_once_with(HELSINKI)
        self.assertTrue(window._auto_connect_done)

    def test_deadline_uses_monotonic_clock(self):
        window = _FakeWindow()
        start = 1000.0
        # A wild system clock must not affect the startup window.
        with mock.patch.object(app_mod.GLib, "timeout_add") as timeout_add, \
                mock.patch.object(app_mod.time, "monotonic", return_value=start), \
                mock.patch.object(app_mod.time, "time", return_value=10 ** 9):
            window._autoconnect_tick()  # starts the deadline, arms a retry
            self.assertFalse(window._auto_connect_done)
            self.assertEqual(timeout_add.call_count, 1)
            self.assertEqual(timeout_add.call_args[0][0],
                             app_mod.AUTOCONNECT_RETRY_MS)
            timeout_add.reset_mock()

            with mock.patch.object(
                app_mod.time, "monotonic",
                return_value=start + app_mod.AUTOCONNECT_TIMEOUT_SECONDS + 1,
            ):
                window._autoconnect_tick()

        self.assertTrue(window._auto_connect_done)
        timeout_add.assert_not_called()
        window.connect_to.assert_not_called()

    def test_ready_data_after_deadline_does_not_connect(self):
        window = _FakeWindow()
        window.status_loaded = True
        window.locations = [HELSINKI]
        window._license_seq = 1
        window._license_inflight = True

        with mock.patch.object(app_mod.GLib, "timeout_add"), \
                mock.patch.object(app_mod.time, "monotonic", return_value=1000.0):
            window._autoconnect_tick()  # deadline starts, license still missing
            self.assertFalse(window._auto_connect_done)

            with mock.patch.object(
                app_mod.time, "monotonic",
                return_value=1000.0 + app_mod.AUTOCONNECT_TIMEOUT_SECONDS + 1,
            ):
                window._license_done(License(logged_in=True), False, 1)

        window.connect_to.assert_not_called()
        self.assertTrue(window._auto_connect_done)

    def test_late_license_response_triggers_autoconnect(self):
        window = _FakeWindow()
        window.status_loaded = True
        window.locations = [HELSINKI]
        self.assertFalse(window.license_loaded)
        window._license_seq = 1
        window._license_inflight = True

        with mock.patch.object(app_mod.GLib, "timeout_add") as timeout_add, \
                mock.patch.object(app_mod.time, "monotonic", return_value=1000.0):
            window._license_done(License(logged_in=True), False, 1)

        self.assertTrue(window.license_loaded)
        timeout_add.assert_not_called()
        window.connect_to.assert_called_once_with(HELSINKI)

    def test_late_license_response_before_status_keeps_waiting(self):
        window = _FakeWindow()
        window.license_loaded = False
        window._license_seq = 1
        window._license_inflight = True
        window.locations = [HELSINKI]

        with mock.patch.object(app_mod.GLib, "timeout_add") as timeout_add:
            window._license_done(License(logged_in=True), False, 1)

        window.connect_to.assert_not_called()
        self.assertTrue(timeout_add.called)
        self.assertEqual(timeout_add.call_args[0][0],
                         app_mod.AUTOCONNECT_RETRY_MS)
        self.assertFalse(window._auto_connect_done)

    def test_connected_or_connecting_is_not_reconnected(self):
        for state in ("connected", "connecting"):
            with self.subTest(state=state):
                window = _FakeWindow()
                window.ready()
                window.status = VpnStatus(state=state)
                with mock.patch.object(app_mod.GLib, "timeout_add"):
                    window._maybe_autoconnect()
                window.connect_to.assert_not_called()
                self.assertTrue(window._auto_connect_done)

    def test_busy_operation_skips_autoconnect(self):
        for attr in ("busy", "_excl_op", "_blocklist_op"):
            with self.subTest(attr=attr):
                window = _FakeWindow()
                window.ready()
                setattr(window, attr, True)
                with mock.patch.object(app_mod.GLib, "timeout_add"):
                    window._maybe_autoconnect()
                window.connect_to.assert_not_called()
                self.assertTrue(window._auto_connect_done)

    def test_autoconnect_runs_once(self):
        window = _FakeWindow()
        window.ready()
        with mock.patch.object(app_mod.GLib, "timeout_add"):
            window._maybe_autoconnect()
            window._maybe_autoconnect()
        window.connect_to.assert_called_once_with(HELSINKI)

    def test_disabled_autoconnect_does_nothing(self):
        window = _FakeWindow()
        window.ready()
        window.store.pref.return_value = False
        with mock.patch.object(app_mod.GLib, "timeout_add") as timeout_add:
            window._maybe_autoconnect()
        window.connect_to.assert_not_called()
        timeout_add.assert_not_called()
        self.assertTrue(window._auto_connect_done)

    def test_after_shutdown_nothing_happens(self):
        for attr, value in (("_shutting_down", True), ("_alive", False)):
            with self.subTest(attr=attr):
                window = _FakeWindow()
                window.ready()
                setattr(window, attr, value)
                with mock.patch.object(app_mod.GLib, "timeout_add") as timeout_add:
                    self.assertFalse(window._maybe_autoconnect())
                window.connect_to.assert_not_called()
                timeout_add.assert_not_called()
                self.assertFalse(window._auto_connect_done)


if __name__ == "__main__":
    unittest.main()

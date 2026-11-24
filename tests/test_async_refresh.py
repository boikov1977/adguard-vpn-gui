#!/usr/bin/python3
"""Async refreshes must ignore stale answers and keep a dropped request.

No display is needed: ``MainWindow`` methods run on a duck-typed stub whose
``run_async`` stores the callback instead of calling it.  A test delivers the
answer later with ``deliver()``, which models a late CLI reply.
"""
from __future__ import annotations

import sys
import time
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import cli  # noqa: E402
from agvpn.app import MainWindow  # noqa: E402
from agvpn.cli import CliError, License, VpnStatus  # noqa: E402


class _FakeWindow:
    """Duck-typed stand-in for the attributes the refresh paths touch."""

    busy_kind = None
    busy = False
    _alive = True
    _shutting_down = False

    @property
    def busy_or_excl(self) -> bool:
        return MainWindow.busy_or_excl.fget(self)

    def __init__(self) -> None:
        self._callbacks: list[tuple] = []
        self._status_inflight = False
        self._status_refresh_pending = False
        self._status_seq = 0
        self._status_event_pending = False
        self._status_failures = 0
        self._next_poll = 0.0
        self.status = VpnStatus(state="unknown")
        self.status_loaded = False
        self._license_inflight = False
        self._license_seq = 0
        self._license_refresh_pending = False
        self.license = License()
        self.license_loaded = False
        self.license_error = False
        self._exclusions_loaded = True
        self._excl_op = False
        self._config_inflight = False
        self._config_seq = 0
        self._config_refresh_pending = None
        self.config = None
        self.cli_data_dir = ""
        self.store = types.SimpleNamespace(
            data={"prefs": {}},
            connected_since=None,
            last_location=None,
            add_activity=mock.Mock(),
            set_last_location=mock.Mock(),
            pref=mock.Mock(return_value=True),
        )
        self.tray_available = False

        self.cli = mock.Mock()
        self.cli.available = True
        self.cli.status = mock.Mock(return_value=VpnStatus(state="connected"))
        self.cli.license = mock.Mock(
            return_value=License(logged_in=True, email="fresh@example"))
        self.cli.config_show = mock.Mock(return_value=cli.AppConfig())
        self.cli.set_setting = mock.Mock()
        self.cli.logout = mock.Mock()
        self.endpoint_cache = mock.Mock()
        self._schedule_status_poll = mock.Mock()
        self._schedule_pending_status_event = mock.Mock()
        self._refresh_ui = mock.Mock()
        self._refresh_static_ui = mock.Mock()
        self._maybe_remember_status_location = mock.Mock()
        self._remember_endpoint = mock.Mock()
        self._notify_state = mock.Mock()
        self._cli_missing = mock.Mock()
        self.set_busy = mock.Mock()
        self.clear_busy = mock.Mock()
        self.show_error = mock.Mock()
        self.hide_banner = mock.Mock()
        self.confirm = mock.Mock(return_value=True)
        self.locations_page = mock.Mock()
        self.account_page = mock.Mock()
        self.settings_page = mock.Mock()
        self.dashboard = mock.Mock()
        self.toast = mock.Mock()
        self._update_account_chip = mock.Mock()
        self._update_exclusion_badge = mock.Mock()
        self._update_exclusion_dns_hint = mock.Mock()
        # Bind the real refresh paths to this stub: they call each other.
        for name in (
            "refresh_status", "_status_done", "_status_error",
            "_run_pending_status", "apply_status",
            "refresh_license", "_license_done", "_license_error",
            "_run_pending_license",
            "refresh_config", "_config_done", "_config_error",
            "_run_pending_config",
            "confirm_logout", "apply_setting", "apply_settings_sequence",
        ):
            setattr(self, name, types.MethodType(getattr(MainWindow, name), self))

    def run_async(self, func, on_success=None, on_error=None) -> None:
        try:
            result = func()
        except Exception as exc:
            self._callbacks.append((on_error, exc))
        else:
            self._callbacks.append((on_success, result))

    def deliver(self, index: int = 0) -> None:
        """Run one queued callback, modelling a late CLI reply."""
        callback, value = self._callbacks.pop(index)
        if callback is not None:
            callback(value)


class StatusRefreshTests(unittest.TestCase):
    def test_direct_apply_invalidates_a_flight(self):
        window = _FakeWindow()
        window.refresh_status()
        self.assertTrue(window._status_inflight)

        window.apply_status(VpnStatus(state="disconnected"))
        self.assertEqual(window.status.state, "disconnected")

        window.deliver()  # late answer of the superseded flight
        self.assertEqual(window.status.state, "disconnected")
        self.assertFalse(window._status_inflight)

    def test_refresh_during_flight_drops_old_answer_and_repeats(self):
        window = _FakeWindow()
        window.cli.status = mock.Mock(side_effect=[
            VpnStatus(state="connected"),
            VpnStatus(state="disconnected"),
        ])
        window.refresh_status()
        window.apply_status(VpnStatus(state="disconnected"))
        window.refresh_status()  # in flight: new generation + pending repeat
        self.assertTrue(window._status_refresh_pending)

        window.deliver()  # stale answer must not overwrite the fresh state
        self.assertEqual(window.status.state, "disconnected")
        self.assertFalse(window._status_refresh_pending)
        self.assertEqual(window.cli.status.call_count, 2)
        self.assertTrue(window._status_inflight)  # pending repeat is running

        window.deliver()
        self.assertEqual(window.status.state, "disconnected")
        self.assertFalse(window._status_inflight)

    def test_stale_error_answer_is_not_applied(self):
        window = _FakeWindow()
        window.cli.status = mock.Mock(side_effect=[
            CliError("adguardvpn-cli not found", kind="missing"),
            VpnStatus(state="disconnected"),
        ])
        window.refresh_status()
        window.refresh_status()  # in flight: pending repeat
        self.assertTrue(window._status_refresh_pending)

        window.deliver()  # stale failure of the superseded flight
        window._cli_missing.assert_not_called()
        self.assertEqual(window._status_failures, 0)
        self.assertEqual(window._next_poll, 0.0)
        self.assertEqual(window.status.state, "unknown")
        self.assertFalse(window._status_refresh_pending)
        self.assertEqual(window.cli.status.call_count, 2)
        self.assertTrue(window._status_inflight)  # pending repeat is running

        window.deliver()
        self.assertEqual(window.status.state, "disconnected")
        self.assertFalse(window._status_inflight)

    def test_pending_refresh_respects_backoff(self):
        window = _FakeWindow()
        window.cli.status = mock.Mock(side_effect=[
            RuntimeError("boom"),
            VpnStatus(state="connected"),
        ])
        window.refresh_status()
        window.deliver()  # fails: backoff is now active
        self.assertGreater(window._next_poll, time.time())

        window.refresh_status()   # manual refresh ignores the backoff
        window.refresh_status()   # in flight: pending repeat
        window.deliver()          # stale answer, no backoff update
        self.assertFalse(window._status_refresh_pending)
        self.assertEqual(window.cli.status.call_count, 2)
        self.assertFalse(window._status_inflight)


class LicenseRefreshTests(unittest.TestCase):
    def test_refresh_during_flight_drops_old_answer_and_repeats(self):
        window = _FakeWindow()
        window.cli.license = mock.Mock(side_effect=[
            License(logged_in=True, email="old@example"),
            License(logged_in=False, email="new@example"),
        ])
        window.refresh_license()
        window.refresh_license()  # in flight: new generation + pending repeat
        self.assertTrue(window._license_refresh_pending)

        window.deliver()  # stale startup answer is not applied at all
        self.assertFalse(window.license.logged_in)
        self.assertFalse(window.license_loaded)
        self.assertEqual(window.cli.license.call_count, 2)

        window.deliver()  # the repeat
        self.assertFalse(window.license.logged_in)
        self.assertEqual(window.license.email, "new@example")
        self.assertTrue(window.license_loaded)
        self.assertFalse(window._license_inflight)

    def test_stale_error_answer_is_not_applied(self):
        window = _FakeWindow()
        window.cli.license = mock.Mock(side_effect=[
            CliError("service unavailable", kind="unknown"),
            License(logged_in=False),
        ])
        window.refresh_license()
        window.refresh_license()  # in flight: pending repeat
        self.assertTrue(window._license_refresh_pending)

        window.deliver()  # stale failure of the superseded flight
        self.assertFalse(window.license_error)
        window.toast.assert_not_called()
        self.assertFalse(window._license_refresh_pending)
        self.assertEqual(window.cli.license.call_count, 2)
        self.assertTrue(window._license_inflight)  # pending repeat is running

        window.deliver()
        self.assertTrue(window.license_loaded)
        self.assertFalse(window.license.logged_in)
        self.assertFalse(window._license_inflight)

    def test_logout_during_startup_refresh_drops_the_stale_login(self):
        window = _FakeWindow()
        window.refresh_status = mock.Mock()  # logout also refreshes the status
        window.cli.license = mock.Mock(side_effect=[
            License(logged_in=True, email="old@example"),
            License(logged_in=False),
        ])
        window.refresh_license()   # startup request is still in flight
        window.confirm_logout()    # the real logout done() asks for a refresh
        self.assertEqual(len(window._callbacks), 2)
        window.deliver(1)          # logout finished
        self.assertTrue(window._license_refresh_pending)
        self.assertFalse(window.license.logged_in)

        window.deliver(0)          # stale startup answer: dropped
        self.assertFalse(window.license.logged_in)
        self.assertTrue(window._license_inflight)
        self.assertEqual(window.cli.license.call_count, 2)

        window.deliver()           # the repeat after logout
        self.assertFalse(window.license.logged_in)
        self.assertTrue(window.license_loaded)
        self.assertFalse(window._license_inflight)


class ConfigRefreshTests(unittest.TestCase):
    def test_refresh_during_flight_drops_old_answer_and_keeps_arguments(self):
        window = _FakeWindow()
        window.cli.config_show = mock.Mock(side_effect=[
            cli.AppConfig(values={"mode": "socks"}),
            cli.AppConfig(values={"mode": "tun"}),
        ])
        callback = mock.Mock()
        window.refresh_config(silent=False, callback=callback)
        window.refresh_config(silent=False, callback=callback)
        self.assertEqual(window._config_refresh_pending, (False, callback))

        window.deliver()  # stale answer: not applied, foreign callback not run
        self.assertIsNone(window.config)
        callback.assert_not_called()
        self.assertEqual(window.cli.config_show.call_count, 2)

        window.deliver()  # the repeat
        self.assertEqual(window.config.values["mode"], "tun")
        callback.assert_called_once()
        self.assertFalse(window._config_inflight)

    def test_stale_error_answer_is_not_applied(self):
        window = _FakeWindow()
        window.cli.config_show = mock.Mock(side_effect=[
            CliError("service unavailable", kind="unknown"),
            cli.AppConfig(values={"mode": "tun"}),
        ])
        callback = mock.Mock()
        window.refresh_config(silent=False, callback=callback)
        window.refresh_config(silent=False, callback=callback)
        self.assertEqual(window._config_refresh_pending, (False, callback))

        window.deliver()  # stale failure: no toast, foreign callback not run
        window.toast.assert_not_called()
        callback.assert_not_called()
        self.assertIsNone(window._config_refresh_pending)
        self.assertEqual(window.cli.config_show.call_count, 2)
        self.assertTrue(window._config_inflight)  # pending repeat is running

        window.deliver()
        self.assertEqual(window.config.values["mode"], "tun")
        callback.assert_called_once()
        self.assertFalse(window._config_inflight)

    def test_apply_setting_invalidates_a_flight(self):
        window = _FakeWindow()
        fresh = cli.AppConfig(values={"mode": "tun"})
        window.cli.config_show = mock.Mock(side_effect=[
            cli.AppConfig(values={"mode": "socks"}), fresh])
        window.refresh_config()
        window.apply_setting("mode", "tun")
        self.assertEqual(len(window._callbacks), 2)

        window.deliver(1)  # the setting was applied directly
        self.assertIs(window.config, fresh)

        window.deliver(0)  # stale refresh answer
        self.assertIs(window.config, fresh)
        self.assertFalse(window._config_inflight)

    def test_apply_settings_sequence_invalidates_a_flight(self):
        window = _FakeWindow()
        fresh = cli.AppConfig(values={"mode": "tun"})
        window.cli.config_show = mock.Mock(side_effect=[
            cli.AppConfig(values={"mode": "socks"}), fresh])
        window.refresh_config()
        window.apply_settings_sequence([("mode", "tun")])
        self.assertEqual(len(window._callbacks), 2)

        window.deliver(1)  # the settings were applied directly
        self.assertIs(window.config, fresh)

        window.deliver(0)  # stale refresh answer
        self.assertIs(window.config, fresh)
        self.assertFalse(window._config_inflight)


if __name__ == "__main__":
    unittest.main()

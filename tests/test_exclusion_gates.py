#!/usr/bin/python3
"""Exclusion operations must lock the CLI like connect/disconnect do.

No display is needed: ``MainWindow`` methods run on a duck-typed stub.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn.app import MainWindow  # noqa: E402
from agvpn.cli import Location, VpnStatus  # noqa: E402
from agvpn.pages import ExclusionsPage  # noqa: E402


class _FakeWindow:
    """Duck-typed stand-in for the attributes the gates touch."""

    busy = False
    busy_kind = None
    _alive = True
    _shutting_down = False

    def __init__(self) -> None:
        self._excl_op = False
        self._excl_inflight = False
        self._exclusions_loaded = False
        self.exclusions = []
        self.exclusion_mode = "general"
        self.status = VpnStatus(state="connected")
        self.status_loaded = True
        self._op_id = 0
        self._op_cancel = None
        self.config = None
        self.store = types.SimpleNamespace(
            data={"prefs": {}},
            last_location=None,
            add_activity=mock.Mock(),
            remember_recent=mock.Mock(),
            recent_keys=[],
        )
        self.tray_available = False

        self.cli = mock.Mock()
        self.cli.available = True
        self.cli.exclusions = mock.Mock(return_value=["a.com"])
        self.cli.exclusion_mode = mock.Mock(return_value="general")
        self.exclusions_page = mock.Mock()
        self.settings_page = mock.Mock()
        self.dashboard = mock.Mock()
        self.toast = mock.Mock()
        self.set_busy = mock.Mock()
        self.clear_busy = mock.Mock()
        self.apply_status = mock.Mock()
        self.current_location = mock.Mock(
            return_value=Location("FI", "Finland", "Helsinki"))
        self._start_connect = mock.Mock()
        self._update_exclusion_badge = mock.Mock()
        self._location_name = MainWindow._location_name
        # Bind the real gate owners to this stub.
        for name in ("disconnect", "toggle_connection", "load_exclusions",
                     "apply_setting", "refresh_all", "_after_exclusions",
                     "open_login_terminal", "check_updates", "add_exclusions"):
            setattr(self, name, types.MethodType(getattr(MainWindow, name), self))

    @property
    def busy_or_excl(self) -> bool:
        return MainWindow.busy_or_excl.fget(self)

    def run_async(self, func, on_success=None, on_error=None) -> None:
        try:
            result = func()
        except Exception as exc:
            if on_error is not None:
                on_error(exc)
        else:
            if on_success is not None:
                on_success(result)


def _page(window) -> ExclusionsPage:
    """Duck-typed ExclusionsPage with only the fields ``_add`` uses."""
    page = ExclusionsPage.__new__(ExclusionsPage)
    page.win = window
    page._busy = False
    page._mode = "general"
    page.entry = mock.Mock()
    page.entry.get_text.return_value = "example.com"
    page.mode_control = mock.Mock()
    page._update_controls = mock.Mock()
    page._update_mode_text = mock.Mock()
    page._on_mode_change = types.MethodType(ExclusionsPage._on_mode_change, page)
    page._add = types.MethodType(ExclusionsPage._add, page)
    return page


class ExclusionGateTests(unittest.TestCase):
    def test_disconnect_is_blocked_during_exclusions(self):
        window = _FakeWindow()
        window._excl_op = True
        window.disconnect()
        window.cli.disconnect.assert_not_called()
        window.set_busy.assert_not_called()
        window.toast.assert_called_once()

    def test_toggle_is_blocked_during_exclusions(self):
        window = _FakeWindow()
        window.disconnect = mock.Mock()
        window.connect_to = mock.Mock()
        window._excl_op = True
        window.toggle_connection()
        window.disconnect.assert_not_called()
        window.connect_to.assert_not_called()
        window.toast.assert_called_once()

    def test_after_exclusions_does_not_reconnect_during_operation(self):
        window = _FakeWindow()
        window._excl_op = True
        window._after_exclusions(True, VpnStatus(state="disconnected"))
        window._start_connect.assert_not_called()

    def test_load_exclusions_force_reloads(self):
        window = _FakeWindow()
        window._exclusions_loaded = True
        window.load_exclusions()
        window.cli.exclusion_mode.assert_not_called()

        window.load_exclusions(force=True)
        window.cli.exclusion_mode.assert_called_once()

    def test_load_exclusions_loads_once_when_not_forced(self):
        window = _FakeWindow()
        window.load_exclusions()
        window.cli.exclusion_mode.assert_called_once()
        window.load_exclusions()
        window.cli.exclusion_mode.assert_called_once()

    def test_add_does_not_leave_the_page_busy(self):
        window = _FakeWindow()
        window._excl_op = True
        page = _page(window)
        page._add()
        self.assertFalse(page._busy)
        page._update_controls.assert_not_called()
        window.cli.add_exclusions.assert_not_called()

    def test_mode_change_is_ignored_while_busy(self):
        window = _FakeWindow()
        window._excl_op = True
        page = _page(window)
        page._on_mode_change("selective")
        self.assertEqual(page._mode, "general")
        page._update_mode_text.assert_not_called()
        page.mode_control.set_value.assert_called_once_with("general")
        window.cli.set_exclusion_mode.assert_not_called()

    def test_login_terminal_is_blocked_during_exclusions(self):
        window = _FakeWindow()
        window._excl_op = True
        window.show_details = mock.Mock()
        with mock.patch("agvpn.app.util.terminal_command") as terminal:
            window.open_login_terminal()
        terminal.assert_not_called()
        window.show_details.assert_not_called()
        window.toast.assert_called_once()

    def test_check_updates_is_blocked_during_exclusions(self):
        window = _FakeWindow()
        window._excl_op = True
        window.check_updates()
        window.set_busy.assert_not_called()
        window.cli.check_update.assert_not_called()

    def test_apply_setting_is_blocked_during_exclusions(self):
        window = _FakeWindow()
        window._excl_op = True
        window.apply_setting("mode", "tun")
        window.cli.set_setting.assert_not_called()
        window.toast.assert_called_once()

    def test_refresh_all_is_blocked_during_exclusions(self):
        window = _FakeWindow()
        window.refresh_status = mock.Mock()
        window._excl_op = True
        window.refresh_all()
        window.refresh_status.assert_not_called()
        window.toast.assert_called_once()


if __name__ == "__main__":
    unittest.main()

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
from agvpn.cli import CliError, License, Location, VpnStatus  # noqa: E402
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
        self._excl_seq = 0
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
        self._maybe_autoconnect = mock.Mock()
        self._location_name = MainWindow._location_name
        # Bind the real gate owners to this stub.
        for name in ("disconnect", "toggle_connection", "load_exclusions",
                     "apply_setting", "refresh_all", "_after_exclusions",
                     "open_login_terminal", "check_updates", "add_exclusions",
                     "remove_exclusion", "confirm_clear_exclusions",
                     "set_exclusion_mode"):
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
    page.reset = types.MethodType(ExclusionsPage.reset, page)
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


class _QueuedWindow(_FakeWindow):
    """A window whose async answers are delivered later by the test."""

    def __init__(self) -> None:
        super().__init__()
        self._callbacks: list[tuple] = []

    def run_async(self, func, on_success=None, on_error=None) -> None:
        self._callbacks.append((on_success, on_error))

    def deliver(self, index: int = 0, payload=None) -> None:
        on_success, _on_error = self._callbacks.pop(index)
        on_success(mock.Mock() if payload is None else payload)

    def deliver_error(self, index: int = 0, error: Exception | None = None) -> None:
        _on_success, on_error = self._callbacks.pop(index)
        on_error(error or CliError("session expired", kind="not_logged_in"))

    def logout(self) -> None:
        """Queue a logout; ``deliver(1)`` then finishes it."""
        self.refresh_license = mock.Mock()
        self.refresh_status = mock.Mock()
        self.locations_page = mock.Mock()
        self.confirm = mock.Mock(return_value=True)
        self.confirm_logout = types.MethodType(MainWindow.confirm_logout, self)
        self.confirm_logout()


class LateExclusionsAnswerTests(unittest.TestCase):
    def test_late_load_does_not_restore_the_previous_account(self):
        window = _QueuedWindow()
        window.load_exclusions()      # the read of the logged-in account
        self.assertTrue(window._excl_inflight)

        window.logout()
        window.deliver(1)             # logout finished: the list was reset
        self.assertFalse(window._exclusions_loaded)
        self.assertFalse(window._excl_inflight)

        window.deliver(0, ("general", ["old.example"]))  # answer of the old account
        self.assertFalse(window._exclusions_loaded)
        self.assertFalse(window._excl_inflight)
        window.exclusions_page.set_data.assert_not_called()
        self.assertEqual(window.exclusions, [])

    def test_reset_unlocks_the_page_when_a_load_was_in_flight(self):
        window = _QueuedWindow()
        page = _page(window)
        page.set_busy = types.MethodType(ExclusionsPage.set_busy, page)
        page.set_data = mock.Mock()
        window.exclusions_page = page

        window.load_exclusions()
        self.assertTrue(page._busy)   # "Добавляем…", controls disabled

        window.logout()
        window.deliver(1)             # the stale answer will never arrive

        self.assertFalse(page._busy)
        self.assertFalse(window._excl_inflight)

    def test_stale_failure_after_reset_is_not_shown(self):
        window = _QueuedWindow()
        page = _page(window)
        page.set_busy = types.MethodType(ExclusionsPage.set_busy, page)
        page.set_data = mock.Mock()
        window.exclusions_page = page
        window.show_error = mock.Mock()
        window._last_page = "exclusions"

        window.load_exclusions()
        window.logout()
        window.deliver(1)             # logout finished: the list was reset
        self.assertFalse(page._busy)

        window.deliver_error(0)       # late failure of the old account

        window.show_error.assert_not_called()
        page.set_data.assert_not_called()
        self.assertFalse(window._excl_inflight)

    def test_late_load_does_not_restore_an_expired_session(self):
        window = _QueuedWindow()
        window.load_exclusions()
        window._license_seq = 1
        window._license_inflight = True
        window.account_page = mock.Mock()
        window._update_account_chip = mock.Mock()
        window._run_pending_license = mock.Mock()
        window._license_done = types.MethodType(MainWindow._license_done, window)

        window._license_done(License(logged_in=False), False, 1)
        self.assertFalse(window._exclusions_loaded)
        self.assertFalse(window._excl_inflight)

        window.deliver(0, ("general", ["old.example"]))
        self.assertFalse(window._exclusions_loaded)
        window.exclusions_page.set_data.assert_not_called()
        self.assertEqual(window.exclusions, [])


class BusyToastTests(unittest.TestCase):
    """A click on a locked exclusions action must not vanish silently."""

    CASES = (
        ("add_exclusions", (["a.com"], "general")),
        ("remove_exclusion", ("a.com", "general")),
        ("confirm_clear_exclusions", ("general", 3)),
        ("set_exclusion_mode", ("selective",)),
    )

    def test_every_busy_action_shows_the_wait_toast(self):
        for name, args in self.CASES:
            with self.subTest(action=name):
                window = _FakeWindow()
                window.busy = True
                window.confirm = mock.Mock(return_value=True)

                getattr(window, name)(*args)

                window.toast.assert_called_once()
                self.assertTrue(window.toast.call_args.kwargs["error"])
                self.assertFalse(window._excl_op)
                window.exclusions_page.set_busy.assert_not_called()


class ExclusionReloadTests(unittest.TestCase):
    def test_logout_resets_the_page_flag(self):
        window = _FakeWindow()
        page = _page(window)
        page._loaded = True
        window.exclusions_page = page
        window.confirm = mock.Mock(return_value=True)
        window.locations_page = mock.Mock()
        window.refresh_license = mock.Mock()
        window.refresh_status = mock.Mock()
        window.confirm_logout = types.MethodType(MainWindow.confirm_logout, window)

        window.confirm_logout()

        self.assertFalse(window._exclusions_loaded)
        self.assertFalse(page._loaded)

    def test_not_logged_in_license_resets_the_page_flag(self):
        window = _FakeWindow()
        page = _page(window)
        page._loaded = True
        window.exclusions_page = page
        window._license_seq = 1
        window._license_inflight = True
        window.account_page = mock.Mock()
        window._update_account_chip = mock.Mock()
        window._run_pending_license = mock.Mock()
        window._license_done = types.MethodType(MainWindow._license_done, window)

        window._license_done(License(logged_in=False), False, 1)

        self.assertFalse(window._exclusions_loaded)
        self.assertFalse(page._loaded)

    def test_shown_after_reset_reloads_the_list(self):
        window = _FakeWindow()
        page = _page(window)
        page._loaded = False
        page.set_data = mock.Mock()
        window.exclusions_page = page
        window.load_blocklist_cache = mock.Mock()
        window.load_exclusions = types.MethodType(MainWindow.load_exclusions, window)
        page.shown = types.MethodType(ExclusionsPage.shown, page)

        page.shown()

        window.cli.exclusion_mode.assert_called_once()
        self.assertTrue(window._exclusions_loaded)

    def test_shown_loaded_page_does_not_reload(self):
        window = _FakeWindow()
        page = _page(window)
        page._loaded = True
        window.load_blocklist_cache = mock.Mock()
        window.load_exclusions = mock.Mock()
        page.shown = types.MethodType(ExclusionsPage.shown, page)

        page.shown()

        window.load_exclusions.assert_not_called()

#!/usr/bin/python3
"""Read-only CLI calls must fail on error instead of returning fake data.

No display and no real CLI are needed: ``_run`` is mocked, and the location
refresh is driven through ``MainWindow.refresh_locations`` with a stub window.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn.app import MainWindow  # noqa: E402
from agvpn.cli import AdGuardCLI, CliError, CliResult, Location  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


def cli_with(output: str, code: int = 0, stderr: str = "") -> AdGuardCLI:
    cli = AdGuardCLI.__new__(AdGuardCLI)
    cli.binary = sys.executable
    cli._version = None
    cli._run = mock.Mock(return_value=CliResult(
        argv=[], code=code, stdout=output, stderr=stderr))
    return cli


class ReadOnlyErrorTests(unittest.TestCase):
    def test_status_error_is_raised(self):
        with self.assertRaises(CliError):
            cli_with("Error: service unavailable\n", code=1).status()

    def test_status_without_login_is_a_state_not_an_error(self):
        status = cli_with("You are not logged in\n", code=11).status()
        self.assertEqual(status.state, "disconnected")
        self.assertIn("not logged in", status.raw)

    def test_disconnected_status_is_accepted_at_any_rc(self):
        # The rc of "status" on a stopped tunnel is unverified, so a readable
        # answer must not be treated as a failure.
        status = cli_with(read("status_disconnected_ansi.txt"), code=1).status()
        self.assertEqual(status.state, "disconnected")

    def test_status_with_alternate_not_logged_in_text(self):
        status = cli_with("Please log in to check status\n", code=11).status()
        self.assertEqual(status.state, "disconnected")

    def test_version_error_is_raised(self):
        with self.assertRaises(CliError):
            cli_with("Error: service unavailable\n", code=1).version()

    def test_version_is_parsed(self):
        self.assertEqual(
            cli_with("adguardvpn-cli v1.8.3\n").version(), "1.8.3")

    def test_config_data_that_looks_like_an_error_is_still_parsed(self):
        text = read("config_show_ansi.txt").replace(
            "SOCKS username: ", "SOCKS username: please log in")
        cfg = cli_with(text).config_show()
        self.assertEqual(cfg.values["socks_username"], "please log in")

    def test_license_data_that_looks_like_an_error_is_still_parsed(self):
        lic = cli_with("Logged in as please log in to the portal\n").license()
        self.assertTrue(lic.logged_in)

    def test_license_error_is_raised(self):
        with self.assertRaises(CliError):
            cli_with("Error: service unavailable\n", code=1).license()

    def test_license_without_login_is_a_valid_answer(self):
        lic = cli_with(
            "Please log in to view your license info\n", code=11
        ).license()
        self.assertFalse(lic.logged_in)

    def test_locations_error_is_raised(self):
        with self.assertRaises(CliError):
            cli_with("Error: service unavailable\n", code=1).list_locations()

    def test_locations_without_login_is_raised_as_not_logged_in(self):
        with self.assertRaises(CliError) as ctx:
            cli_with(
                "Please log in to get the list of available locations\n", code=11
            ).list_locations()
        self.assertEqual(ctx.exception.kind, "not_logged_in")

    def test_config_error_is_raised(self):
        with self.assertRaises(CliError):
            cli_with("Error: service unavailable\n", code=1).config_show()


class CheckUpdateTests(unittest.TestCase):
    def test_unavailable_text_is_not_an_update(self):
        # "unavailable" contains "available": the old code reported an update.
        with self.assertRaises(CliError):
            cli_with("Error: service unavailable\n", code=1).check_update()

    def test_unavailable_text_with_rc_zero_is_not_an_update(self):
        with self.assertRaises(CliError):
            cli_with("Error: service unavailable\n").check_update()

    def test_rc_17_means_no_update(self):
        available, text = cli_with(
            "You are using the latest version\n", code=17
        ).check_update()
        self.assertFalse(available)
        self.assertIn("latest version", text)

    def test_new_version_is_reported(self):
        available, _text = cli_with(
            "A new version v1.9.0 is now available\n"
        ).check_update()
        self.assertTrue(available)

    def test_unknown_output_is_an_error(self):
        with self.assertRaises(CliError):
            cli_with("The following argument was not expected: --bogus").check_update()


class _FakeWindow:
    """Duck-typed stand-in for the attributes refresh_locations touches."""

    busy = False
    _locations_inflight = False

    def __init__(self, cli: AdGuardCLI) -> None:
        self.cli = cli
        self.locations = [Location("FI", "Finland", "Helsinki")]
        self.loc_cache = mock.Mock()
        self.locations_page = mock.Mock()
        self.dashboard = mock.Mock()
        self.toast = mock.Mock()
        self.show_error = mock.Mock()
        self._update_location_widgets = mock.Mock()
        self._rebuild_tray_locations = mock.Mock()

    def run_async(self, job, done=None, failed=None) -> None:
        try:
            payload = job()
        except Exception as exc:
            if failed is None:
                raise
            failed(exc)
        else:
            if done is not None:
                done(payload)

    def set_busy(self, *_args, **_kwargs) -> None:
        pass

    def clear_busy(self, *_args, **_kwargs) -> None:
        pass


class RefreshLocationsTests(unittest.TestCase):
    def test_error_keeps_the_previous_locations(self):
        window = _FakeWindow(cli_with("Error: service unavailable\n", code=1))
        MainWindow.refresh_locations(window, silent=False)
        window.loc_cache.save.assert_not_called()
        window.show_error.assert_called_once()
        self.assertEqual([loc.city for loc in window.locations], ["Helsinki"])

    def test_empty_answer_does_not_wipe_the_cache(self):
        window = _FakeWindow(cli_with(""))
        MainWindow.refresh_locations(window, silent=False)
        window.loc_cache.save.assert_not_called()
        window.show_error.assert_called_once()
        self.assertEqual([loc.city for loc in window.locations], ["Helsinki"])

    def test_success_saves_the_new_list(self):
        window = _FakeWindow(cli_with(
            "LV    Latvia               Riga                           19\n"
        ))
        MainWindow.refresh_locations(window, silent=True)
        window.loc_cache.save.assert_called_once()
        self.assertEqual([loc.city for loc in window.locations], ["Riga"])


if __name__ == "__main__":
    unittest.main()

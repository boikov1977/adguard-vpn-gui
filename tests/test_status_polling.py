#!/usr/bin/python3
"""Status polling helpers.  Run:  python3 -m unittest discover -s tests -v

The fallback interval and the file-monitor name filter are pure functions, so
these tests need neither a display nor GTK widgets.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn.app import (  # noqa: E402
    STATUS_POLL_HIDDEN_MS,
    STATUS_POLL_VISIBLE_MS,
    is_status_file_event,
    status_poll_interval_ms,
)


class StatusPollIntervalTests(unittest.TestCase):
    def test_visible_window_polls_every_30_seconds(self):
        self.assertEqual(status_poll_interval_ms(False), 30_000)
        self.assertEqual(status_poll_interval_ms(False), STATUS_POLL_VISIBLE_MS)

    def test_tray_hidden_polls_every_120_seconds(self):
        self.assertEqual(status_poll_interval_ms(True), 120_000)
        self.assertEqual(status_poll_interval_ms(True), STATUS_POLL_HIDDEN_MS)

    def test_hidden_interval_is_much_cheaper(self):
        self.assertGreater(STATUS_POLL_HIDDEN_MS, STATUS_POLL_VISIBLE_MS)
        self.assertGreaterEqual(STATUS_POLL_HIDDEN_MS // STATUS_POLL_VISIBLE_MS, 4)

    def test_intervals_are_plain_ints_for_glib(self):
        self.assertIs(type(status_poll_interval_ms(False)), int)
        self.assertIs(type(status_poll_interval_ms(True)), int)
        self.assertGreater(status_poll_interval_ms(False), 0)
        self.assertGreater(status_poll_interval_ms(True), 0)


class StatusFileEventTests(unittest.TestCase):
    def test_pid_file_is_a_status_event(self):
        self.assertTrue(is_status_file_event("vpn.pid"))

    def test_socket_file_is_a_status_event(self):
        self.assertTrue(is_status_file_event("vpn.socket"))

    def test_full_paths_are_reduced_to_basename(self):
        self.assertTrue(is_status_file_event("/home/user/.local/share/adguardvpn-cli/vpn.pid"))
        self.assertTrue(is_status_file_event("/run/adguardvpn-cli/vpn.socket"))

    def test_bytes_basenames_are_supported(self):
        self.assertTrue(is_status_file_event(b"vpn.pid"))

    def test_other_cli_files_are_ignored(self):
        for name in (
            "tunnel.log", "vpn.log", "settings.json", "vpn.pid.bak",
            "vpn.socket.tmp", "my-vpn.pid", "vpn.pid/child", "credentials",
        ):
            with self.subTest(name=name):
                self.assertFalse(is_status_file_event(name))

    def test_empty_or_missing_names_are_ignored(self):
        for name in ("", None):
            with self.subTest(name=name):
                self.assertFalse(is_status_file_event(name))

    def test_matching_is_case_sensitive(self):
        self.assertFalse(is_status_file_event("VPN.PID"))
        self.assertFalse(is_status_file_event("Vpn.Socket"))


if __name__ == "__main__":
    unittest.main()

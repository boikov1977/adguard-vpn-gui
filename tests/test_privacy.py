#!/usr/bin/python3
"""Privacy and export helper tests.  Run:  python3 -m unittest discover -s tests -v

No widgets are created, so these run headless.
"""
from __future__ import annotations

import sys
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn.app import logs_export_filename  # noqa: E402
from agvpn.cli import format_argv_for_log  # noqa: E402


class ArgvPrivacyTests(unittest.TestCase):
    def test_short_command_is_kept_as_is(self):
        self.assertEqual(
            format_argv_for_log(["connect", "-l", "Amsterdam"]),
            "connect -l Amsterdam",
        )

    def test_long_exclusion_list_is_replaced_with_count(self):
        # add_exclusions() passes all domains as one space-separated argument.
        domains = " ".join(f"domain{i}.example" for i in range(386))
        self.assertEqual(
            format_argv_for_log(["site-exclusions", "add", domains]),
            "site-exclusions add <386 args>",
        )

    def test_long_single_token_is_clipped(self):
        text = format_argv_for_log(["set-socks-username", "x" * 500])
        self.assertLessEqual(len(text), 300)
        self.assertIn("…", text)
        self.assertNotIn("x" * 100, text)

    def test_total_length_is_bounded_for_many_arguments(self):
        text = format_argv_for_log(["cmd", *[f"arg{i}" for i in range(100)]])
        self.assertLessEqual(len(text), 300)
        self.assertIn("<", text)
        self.assertIn("args>", text)


class ExportFilenameTests(unittest.TestCase):
    def test_timestamped_zip_name(self):
        moment = datetime(2026, 11, 22, 9, 5, 3)
        self.assertEqual(
            logs_export_filename(moment),
            "adguard-vpn-logs-20261122-090503.zip",
        )


if __name__ == "__main__":
    unittest.main()

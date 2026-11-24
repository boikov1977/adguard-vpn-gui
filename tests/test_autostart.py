#!/usr/bin/python3
"""Headless tests for the autostart desktop entry.

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn.app import MainWindow  # noqa: E402


class DesktopQuoteTests(unittest.TestCase):
    def test_plain_path_is_quoted(self):
        self.assertEqual(
            MainWindow._desktop_quote("/usr/bin/python3"),
            '"/usr/bin/python3"',
        )

    def test_desktop_entry_special_characters_are_escaped(self):
        # install.sh already escapes these; the GUI autostart writer must
        # produce the same Exec syntax for paths containing them.
        quoted = MainWindow._desktop_quote('$HOME/100%/`x`/"q"\\z')
        self.assertTrue(quoted.startswith('"') and quoted.endswith('"'))
        self.assertIn("\\$HOME", quoted)
        self.assertIn("100%%", quoted)
        self.assertIn("\\`x\\`", quoted)
        self.assertIn('\\"q\\"', quoted)
        self.assertIn("\\\\z", quoted)


if __name__ == "__main__":
    unittest.main()

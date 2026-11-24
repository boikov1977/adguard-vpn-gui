#!/usr/bin/python3
"""Headless tests for the autostart desktop entry.

Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import sys
import tempfile
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import util  # noqa: E402
from agvpn.app import MainWindow, autostart_try_exec  # noqa: E402


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


class WriteAutostartTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.dir = Path(self._tmp.name)
        patcher = mock.patch.object(util, "AUTOSTART_DIR", self.dir)
        patcher.start()
        self.addCleanup(patcher.stop)
        # _write_autostart only needs the quoting helper from the window.
        self.window = types.SimpleNamespace(_desktop_quote=MainWindow._desktop_quote)

    def entry(self) -> dict[str, str]:
        text = (self.dir / "adguard-vpn-gui.desktop").read_text(encoding="utf-8")
        return dict(
            line.split("=", 1) for line in text.splitlines() if "=" in line
        )

    def test_enabled_entry_has_an_executable_try_exec(self):
        self.assertTrue(MainWindow._write_autostart(self.window, True))
        fields = self.entry()
        self.assertIn("Exec", fields)
        self.assertTrue(fields.get("TryExec"), "TryExec must be present and non-empty")
        self.assertTrue(os.access(fields["TryExec"], os.X_OK),
                        "TryExec must point at an executable on disk")

    def test_disabled_entry_is_removed(self):
        self.assertTrue(MainWindow._write_autostart(self.window, True))
        self.assertTrue(MainWindow._write_autostart(self.window, False))
        self.assertFalse((self.dir / "adguard-vpn-gui.desktop").exists())

    def test_try_exec_falls_back_to_the_interpreter(self):
        # No launcher and no executable script: TryExec must still name a
        # real file, otherwise the desktop ignores the entry entirely.
        with mock.patch("agvpn.app.os.access", return_value=False):
            self.assertEqual(autostart_try_exec(Path("/nonexistent/script")),
                             sys.executable)


if __name__ == "__main__":
    unittest.main()

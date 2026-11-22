#!/usr/bin/python3
"""Headless tests for log privacy and terminal detection.

Run:  python3 -m unittest discover -s tests -v

No display, no real terminal and no network are needed.
"""
from __future__ import annotations

import logging
import os
import stat
import sys
import tempfile
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import util  # noqa: E402


class LoggingPermissionsTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.state = Path(self._tmp.name) / "state"
        self.root = logging.getLogger()
        self._saved_handlers = list(self.root.handlers)
        self._saved_level = self.root.level
        self.root.handlers = []
        self.addCleanup(self._restore)
        patcher = mock.patch.object(util, "STATE_DIR", self.state)
        patcher.start()
        self.addCleanup(patcher.stop)
        ready = mock.patch.object(util, "_log_ready", False)
        ready.start()
        self.addCleanup(ready.stop)
        env = mock.patch.dict(os.environ, {}, clear=False)
        env.start()
        self.addCleanup(env.stop)
        os.environ.pop("AGVPN_GUI_DEBUG", None)

    def _restore(self) -> None:
        for handler in self.root.handlers:
            if handler not in self._saved_handlers:
                handler.close()
        self.root.handlers = self._saved_handlers
        self.root.setLevel(self._saved_level)
        util._log_ready = False

    def test_state_dir_is_private_even_when_it_already_exists(self):
        self.state.mkdir(mode=0o755, parents=True)
        os.chmod(self.state, 0o755)

        util.setup_logging()

        self.assertEqual(stat.S_IMODE(self.state.stat().st_mode), 0o700)

    def test_log_file_is_private(self):
        util.setup_logging()
        log_path = self.state / "app.log"

        self.assertTrue(log_path.is_file())
        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)

    def test_existing_log_file_is_tightened(self):
        self.state.mkdir(mode=0o755, parents=True)
        log_path = self.state / "app.log"
        log_path.write_text("old\n", encoding="utf-8")
        os.chmod(log_path, 0o644)

        util.setup_logging()

        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)

    def test_rotation_settings_are_unchanged(self):
        util.setup_logging()
        handlers = [
            h for h in logging.getLogger().handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        ]
        self.assertEqual(len(handlers), 1)
        self.assertEqual(handlers[0].maxBytes, 1_500_000)
        self.assertEqual(handlers[0].backupCount, 2)

    def test_rollover_keeps_log_and_backups_private(self):
        util.setup_logging()
        handler = next(
            h for h in logging.getLogger().handlers
            if isinstance(h, logging.handlers.RotatingFileHandler)
        )
        # Force the worst case: the new current log would be 0644 with umask.
        old_umask = os.umask(0o022)
        try:
            handler.doRollover()
        finally:
            os.umask(old_umask)

        log_path = self.state / "app.log"
        backup = self.state / "app.log.1"
        self.assertTrue(backup.is_file())
        self.assertEqual(stat.S_IMODE(log_path.stat().st_mode), 0o600)
        self.assertEqual(stat.S_IMODE(backup.stat().st_mode), 0o600)

        old_umask = os.umask(0o022)
        try:
            handler.doRollover()
        finally:
            os.umask(old_umask)

        for path in (log_path, backup, self.state / "app.log.2"):
            with self.subTest(path=path.name):
                self.assertTrue(path.is_file())
                self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)


class TerminalCommandTests(unittest.TestCase):
    def _run(self, available: dict[str, str], terminal: str | None = None):
        env = {"PATH": "/usr/bin"}
        if terminal is not None:
            env["TERMINAL"] = terminal
        with mock.patch.dict(os.environ, env, clear=True):
            with mock.patch.object(util.shutil, "which", side_effect=available.get):
                return util.terminal_command()

    def test_returns_none_without_any_terminal(self):
        self.assertIsNone(self._run({}))

    def test_candidate_priority_and_arguments(self):
        cases = {
            "x-terminal-emulator": ["/usr/bin/x-terminal-emulator", "-e", "bash", "-lc"],
            "gnome-terminal": ["/usr/bin/gnome-terminal", "--", "bash", "-lc"],
            "mate-terminal": ["/usr/bin/mate-terminal", "-x", "bash", "-lc"],
            "xfce4-terminal": ["/usr/bin/xfce4-terminal", "-x", "bash", "-lc"],
            "konsole": ["/usr/bin/konsole", "-e", "bash", "-lc"],
            "tilix": ["/usr/bin/tilix", "-e", "bash", "-lc"],
            "kitty": ["/usr/bin/kitty", "-e", "bash", "-lc"],
            "alacritty": ["/usr/bin/alacritty", "-e", "bash", "-lc"],
            "wezterm": ["/usr/bin/wezterm", "start", "--", "bash", "-lc"],
            "xterm": ["/usr/bin/xterm", "-e", "bash", "-lc"],
        }
        for name, expected in cases.items():
            with self.subTest(name=name):
                path = f"/usr/bin/{name}"
                self.assertEqual(self._run({name: path}), expected)

    def test_x_terminal_emulator_wins_over_xterm(self):
        argv = self._run({
            "x-terminal-emulator": "/usr/bin/x-terminal-emulator",
            "xterm": "/usr/bin/xterm",
        })
        self.assertEqual(argv, ["/usr/bin/x-terminal-emulator", "-e", "bash", "-lc"])

    def test_terminal_override_wins(self):
        argv = self._run(
            {"kitty": "/usr/bin/kitty", "gnome-terminal": "/usr/bin/gnome-terminal"},
            terminal="kitty",
        )
        self.assertEqual(argv, ["/usr/bin/kitty", "-e", "bash", "-lc"])

    def test_terminal_override_with_its_own_arguments(self):
        argv = self._run(
            {"kitty": "/usr/bin/kitty"},
            terminal="kitty --single-instance",
        )
        self.assertEqual(
            argv,
            ["/usr/bin/kitty", "--single-instance", "-e", "bash", "-lc"],
        )

    def test_terminal_override_does_not_duplicate_existing_flag(self):
        argv = self._run({"alacritty": "/usr/bin/alacritty"}, terminal="alacritty -e")
        self.assertEqual(argv, ["/usr/bin/alacritty", "-e", "bash", "-lc"])

    def test_terminal_override_wezterm_start_gets_separator(self):
        argv = self._run({"wezterm": "/usr/bin/wezterm"}, terminal="wezterm start")
        self.assertEqual(
            argv,
            ["/usr/bin/wezterm", "start", "--", "bash", "-lc"],
        )

    def test_terminal_override_with_absolute_path(self):
        argv = self._run(
            {"/opt/tty/mytty": "/opt/tty/mytty"},
            terminal="/opt/tty/mytty --fullscreen",
        )
        self.assertEqual(
            argv,
            ["/opt/tty/mytty", "--fullscreen", "-e", "bash", "-lc"],
        )

    def test_malformed_terminal_falls_back_to_candidates(self):
        argv = self._run({"xterm": "/usr/bin/xterm"}, terminal="kitty 'unclosed")
        self.assertEqual(argv, ["/usr/bin/xterm", "-e", "bash", "-lc"])

    def test_missing_terminal_executable_falls_back_to_candidates(self):
        argv = self._run({"xterm": "/usr/bin/xterm"}, terminal="does-not-exist")
        self.assertEqual(argv, ["/usr/bin/xterm", "-e", "bash", "-lc"])

    def test_empty_terminal_is_ignored(self):
        argv = self._run({"xterm": "/usr/bin/xterm"}, terminal="   ")
        self.assertEqual(argv, ["/usr/bin/xterm", "-e", "bash", "-lc"])


if __name__ == "__main__":
    unittest.main()

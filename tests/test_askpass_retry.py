#!/usr/bin/python3
"""Headless tests for the askpass retry marker file.

The UI module imports GTK at import time, so each check runs in a short-lived
Python process with a tiny ``gi`` stub on sys.path.  Only temporary files are
touched; no display is required.
"""
from __future__ import annotations

import os
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent

STUB_GI = """\
def require_version(*_args, **_kwargs):
    return None
"""

STUB_REPOSITORY = """\
class _Dummy:
    pass

Gdk = GLib = Gtk = _Dummy
"""

HELPER = r"""
import os
import stat
import sys
import time

sys.path.insert(0, os.environ["STUB_GI"])
sys.path.insert(0, os.environ["PROJECT_ROOT"])

import agvpn.askpass as askpass

askpass.RETRY_FILE = os.environ.get("RETRY_FILE", askpass.RETRY_FILE)
action = os.environ["ACTION"]

if action == "mark":
    print(askpass._mark_attempt())
    print(askpass._mark_attempt())
    print(oct(stat.S_IMODE(os.stat(askpass.RETRY_FILE).st_mode)))
    with open(askpass.RETRY_FILE, encoding="utf-8") as fh:
        print(fh.read().strip())
elif action == "symlink":
    print(askpass._mark_attempt())
elif action == "stale":
    print(askpass._mark_attempt())
    with open(askpass.RETRY_FILE, encoding="utf-8") as fh:
        print(fh.read().strip())
elif action == "mark_default":
    print(askpass.RETRY_FILE)
    print(askpass._mark_attempt())
elif action == "clear":
    askpass._clear_attempt()
    print(os.path.exists(askpass.RETRY_FILE))
    askpass._clear_attempt()  # must stay best-effort when already gone
    print("ok")
else:
    raise SystemExit(f"unknown action: {action}")
"""


class AskpassRetryTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.stub = self.tmp / "stub"
        (self.stub / "gi").mkdir(parents=True)
        (self.stub / "gi" / "__init__.py").write_text(STUB_GI, encoding="utf-8")
        (self.stub / "gi" / "repository.py").write_text(
            STUB_REPOSITORY, encoding="utf-8"
        )
        self.retry_file = self.tmp / "run" / "agvpn-gui-askpass-attempt"
        self.retry_file.parent.mkdir()
        self.helper = self.tmp / "helper.py"
        self.helper.write_text(HELPER, encoding="utf-8")

    def run_helper(self, action: str) -> subprocess.CompletedProcess:
        env = os.environ.copy()
        env.update({
            "STUB_GI": str(self.stub),
            "PROJECT_ROOT": str(ROOT),
            "RETRY_FILE": str(self.retry_file),
            "ACTION": action,
        })
        return subprocess.run(
            [sys.executable, str(self.helper)],
            env=env,
            capture_output=True,
            text=True,
            timeout=30,
        )

    def test_marker_is_private_timestamp_and_detects_retry(self):
        result = self.run_helper("mark")
        self.assertEqual(result.returncode, 0, result.stderr)
        lines = result.stdout.splitlines()
        self.assertEqual(lines[0], "False", "first attempt must not look like a retry")
        self.assertEqual(lines[1], "True", "second attempt must look like a retry")
        self.assertEqual(lines[2], "0o600")
        # Only a timestamp is stored; no credentials.
        self.assertAlmostEqual(float(lines[3]), time.time(), delta=60)

    def test_existing_loose_marker_is_tightened(self):
        self.retry_file.write_text("1.0", encoding="utf-8")
        os.chmod(self.retry_file, 0o644)

        result = self.run_helper("mark")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines()[2], "0o600")
        self.assertEqual(
            stat.S_IMODE(self.retry_file.stat().st_mode),
            0o600,
        )

    def test_symlink_is_not_followed(self):
        target = self.tmp / "target"
        target.write_text("do not touch", encoding="utf-8")
        os.chmod(target, 0o644)
        os.symlink(target, self.retry_file)

        result = self.run_helper("symlink")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.retry_file.is_symlink())
        self.assertEqual(target.read_text(encoding="utf-8"), "do not touch")

    def test_stale_marker_is_refreshed_but_not_reported_as_retry(self):
        self.retry_file.write_text("1.0", encoding="utf-8")
        old = time.time() - 3600
        os.utime(self.retry_file, (old, old))

        result = self.run_helper("stale")

        self.assertEqual(result.returncode, 0, result.stderr)
        retry, marker = result.stdout.splitlines()
        self.assertEqual(retry, "False")
        self.assertAlmostEqual(float(marker), time.time(), delta=60)

    def test_cli_resets_the_marker_askpass_writes(self):
        # No RETRY_FILE override here: askpass and cli must compute one path.
        runtime = self.tmp / "runtime"
        runtime.mkdir()
        env = os.environ.copy()
        env.update({
            "STUB_GI": str(self.stub),
            "PROJECT_ROOT": str(ROOT),
            "XDG_RUNTIME_DIR": str(runtime),
            "ACTION": "mark_default",
        })
        for key in ("SUDO_UID", "PKEXEC_UID", "RETRY_FILE"):
            env.pop(key, None)
        result = subprocess.run(
            [sys.executable, str(self.helper)],
            env=env, capture_output=True, text=True, timeout=30,
        )
        self.assertEqual(result.returncode, 0, result.stderr)
        marker = Path(result.stdout.splitlines()[0])
        self.assertEqual(marker.parent, runtime)
        self.assertTrue(marker.exists())

        sys.path.insert(0, str(ROOT))
        from agvpn.cli import AdGuardCLI

        with mock.patch.dict(os.environ, {"XDG_RUNTIME_DIR": str(runtime)}):
            os.environ.pop("SUDO_UID", None)
            os.environ.pop("PKEXEC_UID", None)
            AdGuardCLI.__new__(AdGuardCLI).reset_askpass_retry_hint()
        self.assertFalse(marker.exists())

    def test_clear_is_best_effort(self):
        self.retry_file.write_text("123", encoding="utf-8")

        result = self.run_helper("clear")

        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(result.stdout.splitlines(), ["False", "ok"])
        self.assertFalse(self.retry_file.exists())


if __name__ == "__main__":
    unittest.main()

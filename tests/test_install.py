#!/usr/bin/python3
"""Installer tests.  Run:  python3 -m unittest discover -s tests -v

Everything runs against a temporary HOME/XDG tree; the real home directory is
never touched.  No display is required.
"""
from __future__ import annotations

import os
import shlex
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
INSTALL = ROOT / "install.sh"
UNINSTALL = ROOT / "uninstall.sh"
APP_ID = "adguard-vpn-gui"


def parse_desktop(path: Path) -> dict[str, str]:
    fields: dict[str, str] = {}
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or line.startswith("["):
            continue
        key, sep, value = line.partition("=")
        if sep:
            fields[key] = value
    return fields


@unittest.skipUnless(shutil.which("bash"), "bash is required")
class InstallerTests(unittest.TestCase):
    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.tmp = Path(self._tmp.name)
        self.home = self.tmp / "home"
        self.data = self.tmp / "data"
        self.config = self.tmp / "config"
        self.cache = self.tmp / "cache"
        self.state = self.tmp / "state"
        for path in (self.home, self.data, self.config, self.cache, self.state):
            path.mkdir(parents=True, exist_ok=True)
        # Keep the tests away from the real GVfs metadata store: install.sh
        # calls `gio set` only when gio exists, so provide a harmless stub.
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        fake_gio = self.bin / "gio"
        fake_gio.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
        fake_gio.chmod(0o755)

    def tearDown(self) -> None:
        self._tmp.cleanup()

    # -- helpers ---------------------------------------------------------
    def env(self) -> dict[str, str]:
        env = os.environ.copy()
        env.update({
            "HOME": str(self.home),
            "XDG_DATA_HOME": str(self.data),
            "XDG_CONFIG_HOME": str(self.config),
            "XDG_CACHE_HOME": str(self.cache),
            "XDG_STATE_HOME": str(self.state),
        })
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        return env

    def run_script(self, script: Path, *args: str, env: dict[str, str] | None = None,
                   stdin=subprocess.DEVNULL) -> subprocess.CompletedProcess:
        return subprocess.run(
            ["bash", str(script), *args],
            env=env or self.env(),
            cwd=str(self.tmp),  # must not depend on the caller's cwd
            stdin=stdin,
            capture_output=True,
            text=True,
        )

    @property
    def menu_file(self) -> Path:
        return self.data / "applications" / f"{APP_ID}.desktop"

    @property
    def desktop_file(self) -> Path:
        return self.home / "Desktop" / f"{APP_ID}.desktop"

    @property
    def autostart_file(self) -> Path:
        return self.config / "autostart" / f"{APP_ID}.desktop"

    def icon_path(self, size: str, suffix: str) -> Path:
        return self.data / "icons" / "hicolor" / size / "apps" / f"{APP_ID}.{suffix}"

    # -- install ---------------------------------------------------------
    def test_install_no_desktop_then_uninstall(self):
        result = self.run_script(INSTALL, "--no-desktop")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.menu_file.is_file())
        self.assertFalse(self.desktop_file.exists(), "--no-desktop must skip the shortcut")
        self.assertFalse(self.autostart_file.exists(), "install must not touch autostart")

        fields = parse_desktop(self.menu_file)
        self.assertEqual(fields["Type"], "Application")
        self.assertEqual(fields["Name"], "AdGuard VPN")
        self.assertEqual(fields["Comment"], "Графический интерфейс для AdGuard VPN CLI")
        self.assertEqual(fields["Path"], str(ROOT))
        self.assertEqual(fields["Icon"], APP_ID)
        self.assertEqual(fields["Terminal"], "false")
        self.assertEqual(fields["Categories"], "Network;Security;")
        self.assertEqual(fields["StartupWMClass"], "AdGuardVPNGui")
        self.assertEqual(fields["Keywords"], "VPN;AdGuard;")

        exec_parts = shlex.split(fields["Exec"])
        self.assertEqual(len(exec_parts), 2)
        self.assertTrue(Path(exec_parts[0]).is_absolute())
        self.assertEqual(Path(exec_parts[1]), ROOT / "adguard-vpn-gui.py")
        self.assertNotIn("%U", fields["Exec"])

        for size, suffix, source in (
            ("scalable", "svg", "icon.svg"),
            ("256x256", "png", "icon-256.png"),
            ("128x128", "png", "icon-128.png"),
            ("64x64", "png", "icon-64.png"),
            ("48x48", "png", "icon-48.png"),
        ):
            installed = self.icon_path(size, suffix)
            self.assertTrue(installed.is_file(), installed)
            self.assertEqual(installed.read_bytes(),
                             (ROOT / "agvpn" / "assets" / source).read_bytes())

        # Second run must simply overwrite, not fail.
        again = self.run_script(INSTALL, "--no-desktop")
        self.assertEqual(again.returncode, 0, again.stderr)
        self.assertEqual(parse_desktop(self.menu_file)["Exec"], fields["Exec"])

        # Prepare files that uninstall must and must not remove.
        self.autostart_file.parent.mkdir(parents=True, exist_ok=True)
        self.autostart_file.write_text("[Desktop Entry]\nType=Application\n", encoding="utf-8")
        app_dirs = (
            self.config / APP_ID,
            self.cache / APP_ID,
            self.state / APP_ID,
        )
        for path in app_dirs:
            path.mkdir(parents=True, exist_ok=True)
            (path / "keep.txt").write_text("keep", encoding="utf-8")

        removed = self.run_script(UNINSTALL)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(self.menu_file.exists())
        self.assertFalse(self.autostart_file.exists())
        for size, suffix in (
            ("scalable", "svg"),
            ("256x256", "png"),
            ("128x128", "png"),
            ("64x64", "png"),
            ("48x48", "png"),
        ):
            self.assertFalse(self.icon_path(size, suffix).exists())
        for path in app_dirs:
            self.assertTrue((path / "keep.txt").is_file(), "data must be kept without --purge")

        # Idempotent second uninstall.
        again = self.run_script(UNINSTALL)
        self.assertEqual(again.returncode, 0, again.stderr)

    def test_desktop_shortcut(self):
        result = self.run_script(INSTALL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.desktop_file.is_file())
        self.assertTrue(os.access(self.desktop_file, os.X_OK))
        menu_fields = parse_desktop(self.menu_file)
        shortcut_fields = parse_desktop(self.desktop_file)
        self.assertEqual(shortcut_fields["Exec"], menu_fields["Exec"])
        self.assertEqual(shortcut_fields["Terminal"], "false")

        removed = self.run_script(UNINSTALL)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(self.desktop_file.exists())

    @unittest.skipUnless(shutil.which("desktop-file-validate"), "desktop-file-validate not installed")
    def test_desktop_file_validates(self):
        result = self.run_script(INSTALL, "--no-desktop")
        self.assertEqual(result.returncode, 0, result.stderr)
        check = subprocess.run(
            ["desktop-file-validate", str(self.menu_file)],
            capture_output=True, text=True,
        )
        self.assertEqual(check.returncode, 0, check.stderr or check.stdout)

    def test_purge_requires_yes_when_not_interactive(self):
        self.assertEqual(self.run_script(INSTALL, "--no-desktop").returncode, 0)
        app_dir = self.config / APP_ID
        app_dir.mkdir(parents=True, exist_ok=True)
        (app_dir / "keep.txt").write_text("keep", encoding="utf-8")

        refused = self.run_script(UNINSTALL, "--purge")
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn("--yes", refused.stderr)
        self.assertTrue(self.menu_file.exists(), "refusal must not delete anything")
        self.assertTrue((app_dir / "keep.txt").is_file())

        purged = self.run_script(UNINSTALL, "--purge", "--yes")
        self.assertEqual(purged.returncode, 0, purged.stderr)
        self.assertFalse(self.menu_file.exists())
        self.assertFalse(app_dir.exists())

    def test_missing_pygobject_is_reported(self):
        fake_bin = self.tmp / "fakebin"
        fake_bin.mkdir()
        fake_python = fake_bin / "python3"
        fake_python.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_python.chmod(0o755)
        env = self.env()
        env["PATH"] = f"{fake_bin}{os.pathsep}{env.get('PATH', '')}"

        result = self.run_script(INSTALL, "--no-desktop", env=env)
        self.assertNotEqual(result.returncode, 0)
        self.assertIn("python3-gi", result.stderr)
        self.assertIn("gir1.2-ayatanaappindicator3-0.1", result.stderr)

    def test_relative_xdg_paths_are_refused(self):
        cwd_app = self.tmp / APP_ID
        cwd_app.mkdir()
        sentinel = cwd_app / "keep.txt"
        sentinel.write_text("keep", encoding="utf-8")

        for name in ("XDG_DATA_HOME", "XDG_CONFIG_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME"):
            with self.subTest(name=name, script="uninstall"):
                env = self.env()
                env[name] = "."
                result = self.run_script(UNINSTALL, "--purge", "--yes", env=env)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("абсолют", result.stderr)
                self.assertTrue(sentinel.is_file(), "cwd must not be touched")
            with self.subTest(name=name, script="install"):
                env = self.env()
                env[name] = "."
                result = self.run_script(INSTALL, "--no-desktop", env=env)
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertIn("абсолют", result.stderr)
                self.assertFalse((self.tmp / "applications").exists())

    def test_desktop_dir_from_user_dirs(self):
        (self.config / "user-dirs.dirs").write_text(
            'XDG_DESKTOP_DIR="$HOME/Рабочий стол"', encoding="utf-8"
        )
        expected = self.home / "Рабочий стол" / f"{APP_ID}.desktop"
        result = self.run_script(INSTALL)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(expected.is_file())
        self.assertFalse((self.home / "Desktop" / f"{APP_ID}.desktop").exists())

        removed = self.run_script(UNINSTALL)
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(expected.exists())

    def test_purge_keeps_official_cli_data(self):
        self.assertEqual(self.run_script(INSTALL, "--no-desktop").returncode, 0)
        cli_dir = self.home / ".local" / "share" / "adguardvpn-cli"
        cli_dir.mkdir(parents=True)
        sentinel = cli_dir / "sentinel.dat"
        sentinel.write_text("official cli data", encoding="utf-8")
        app_dir = self.config / APP_ID
        app_dir.mkdir(parents=True)
        (app_dir / "keep.txt").write_text("keep", encoding="utf-8")

        purged = self.run_script(UNINSTALL, "--purge", "--yes")
        self.assertEqual(purged.returncode, 0, purged.stderr)
        self.assertFalse(app_dir.exists())
        self.assertTrue(sentinel.is_file(), "official CLI data must survive --purge")

    def test_home_missing_fails(self):
        env = self.env()
        env.pop("HOME", None)
        for script in (INSTALL, UNINSTALL):
            with self.subTest(script=script.name):
                result = self.run_script(script, env=env)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn("HOME", result.stderr)

    def test_failing_gio_does_not_fail_install(self):
        bad_bin = self.tmp / "badbin"
        bad_bin.mkdir()
        fake_gio = bad_bin / "gio"
        fake_gio.write_text("#!/bin/sh\nexit 1\n", encoding="utf-8")
        fake_gio.chmod(0o755)
        env = self.env()
        env["PATH"] = f"{bad_bin}{os.pathsep}{env.get('PATH', '')}"

        result = self.run_script(INSTALL, env=env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(self.desktop_file.is_file())

    def test_project_path_with_spaces_dollar_and_percent(self):
        special = self.tmp / "project $x%y"
        special.mkdir()
        shutil.copy2(INSTALL, special / "install.sh")
        shutil.copy2(UNINSTALL, special / "uninstall.sh")
        (special / "adguard-vpn-gui.py").symlink_to(ROOT / "adguard-vpn-gui.py")
        (special / "agvpn").symlink_to(ROOT / "agvpn", target_is_directory=True)

        result = self.run_script(special / "install.sh", "--no-desktop")
        self.assertEqual(result.returncode, 0, result.stderr)
        fields = parse_desktop(self.menu_file)
        launcher = str(special / "adguard-vpn-gui.py")
        escaped = (launcher.replace("\\", "\\\\").replace('"', '\\"')
                   .replace("`", "\\`").replace("$", "\\$").replace("%", "%%"))
        self.assertIn(f'"{escaped}"', fields["Exec"])
        self.assertIn("\\$", fields["Exec"])
        self.assertIn("%%", fields["Exec"])

        if shutil.which("desktop-file-validate"):
            check = subprocess.run(
                ["desktop-file-validate", str(self.menu_file)],
                capture_output=True, text=True,
            )
            self.assertEqual(check.returncode, 0, check.stderr or check.stdout)

        removed = self.run_script(special / "uninstall.sh")
        self.assertEqual(removed.returncode, 0, removed.stderr)
        self.assertFalse(self.menu_file.exists())


if __name__ == "__main__":
    unittest.main()

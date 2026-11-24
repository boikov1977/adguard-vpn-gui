#!/usr/bin/python3
"""Maintenance-script checks for the .deb build.

Everything reads packaging/build-deb.sh, so no root and no dpkg are needed.
Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import os
import re
import shutil
import subprocess
import tempfile
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "packaging" / "build-deb.sh"
APP = ROOT / "agvpn" / "app.py"

PACKAGE_LIB = "/usr/lib/adguard-vpn-gui"
ENTRY = "adguard-vpn-gui.desktop"


def maintenance_script(name: str) -> str:
    """Return the body of the heredoc that writes DEBIAN/<name>."""
    text = BUILD.read_text(encoding="utf-8")
    match = re.search(
        r"cat > \"\$CTRL/" + name + r"\" <<'EOF'\n(.*?)\nEOF\n", text, re.S
    )
    if match is None:
        raise AssertionError(f"no {name} heredoc in build-deb.sh")
    return match.group(1)


@unittest.skipUnless(shutil.which("bash"), "bash is required")
class DebMaintenanceScriptTests(unittest.TestCase):
    def test_build_script_parses(self):
        result = subprocess.run(["bash", "-n", str(BUILD)], capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)

    def test_generated_scripts_parse(self):
        for name in ("postinst", "postrm"):
            result = subprocess.run(["sh", "-n"], input=maintenance_script(name),
                                    capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, f"{name}: {result.stderr}")

    def test_postrm_removes_the_compileall_cache(self):
        # postinst compiles into /usr/lib as root, so those .pyc files are not
        # in the package manifest and only postrm can delete them.
        postrm = maintenance_script("postrm")
        self.assertIn("compileall", maintenance_script("postinst"))
        self.assertIn("__pycache__", postrm)
        self.assertIn("rm -rf", postrm)

    def test_postrm_removes_the_autostart_entry(self):
        # Name written by MainWindow._write_autostart.
        self.assertIn(f'"{ENTRY}"', APP.read_text(encoding="utf-8"))
        postrm = maintenance_script("postrm")
        self.assertIn("/home/*/.config/autostart", postrm)
        self.assertIn(f'"$dir/{ENTRY}"', postrm)
        self.assertIn("rm -f", postrm)

    def test_postrm_cleanup_is_not_run_on_upgrade(self):
        postrm = maintenance_script("postrm")
        case = re.search(r'case "\$1" in\n(.*?)\n\s*esac', postrm, re.S)
        self.assertIsNotNone(case, "postrm must branch on the dpkg action")
        self.assertIn("remove|purge)", case.group(1))


@unittest.skipUnless(shutil.which("sh"), "sh is required")
class PostrmBehaviourTests(unittest.TestCase):
    """Run the extracted postrm against a throw-away tree.

    The system paths are rewritten to the temporary tree before execution and
    external cache tools are stubbed in PATH, so nothing outside is touched.
    """

    def setUp(self) -> None:
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
        self.lib = self.tmp / "lib"
        self.homes = self.tmp / "homes"
        self.root_home = self.tmp / "root-home"
        self.bin = self.tmp / "bin"
        self.bin.mkdir()
        for tool in ("gtk-update-icon-cache", "update-desktop-database"):
            stub = self.bin / tool
            stub.write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
            stub.chmod(0o755)

    # -- helpers ---------------------------------------------------------
    def run_postrm(self, action: str) -> subprocess.CompletedProcess:
        body = maintenance_script("postrm")
        # These literals are what redirects the real cleanup into self.tmp;
        # a script whose paths moved (e.g. to plain /usr/lib) must not run.
        self.assertIn(f"find {PACKAGE_LIB} ", body)
        self.assertIn("/home/*/.config/autostart", body)
        self.assertIn("/root/.config/autostart", body)
        sandboxed = (
            body.replace(PACKAGE_LIB, str(self.lib))
            .replace("/home/*", f"{self.homes}/*")
            .replace("/root/.config", f"{self.root_home}/.config")
        )
        script = self.tmp / "postrm"
        script.write_text(sandboxed, encoding="utf-8")
        env = os.environ.copy()
        env["PATH"] = f"{self.bin}{os.pathsep}{env.get('PATH', '')}"
        return subprocess.run(["sh", str(script), action], env=env,
                              capture_output=True, text=True)

    def reset(self) -> None:
        for top in (self.lib, self.homes, self.root_home):
            shutil.rmtree(top, ignore_errors=True)

    def populate(self) -> None:
        for relative in (
            "lib/__pycache__/x.pyc",
            "lib/agvpn/__pycache__/mod.pyc",
            "lib/agvpn/mod.py",
            "homes/alice/.config/autostart/adguard-vpn-gui.desktop",
            "homes/bob/.config/autostart/adguard-vpn-gui.desktop",
            "homes/bob/.config/autostart/other.desktop",
            "root-home/.config/autostart/adguard-vpn-gui.desktop",
        ):
            path = self.tmp / relative
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_text("x", encoding="utf-8")

    def snapshot(self) -> list[str]:
        files = []
        for top in (self.lib, self.homes, self.root_home):
            if top.exists():
                files += [str(p.relative_to(self.tmp)) for p in top.rglob("*") if p.is_file()]
        return sorted(files)

    # -- tests -----------------------------------------------------------
    def test_upgrade_removes_nothing(self):
        self.populate()
        before = self.snapshot()
        result = self.run_postrm("upgrade")
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.snapshot(), before)

    def test_remove_and_purge_clean_only_our_files(self):
        expected = [
            "homes/bob/.config/autostart/other.desktop",
            "lib/agvpn/mod.py",
        ]
        for action in ("remove", "purge"):
            with self.subTest(action=action):
                self.reset()
                self.populate()
                result = self.run_postrm(action)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.snapshot(), expected)

    def test_missing_paths_exit_zero(self):
        for action in ("remove", "purge"):
            with self.subTest(action=action):
                self.reset()
                result = self.run_postrm(action)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertEqual(self.snapshot(), [])


if __name__ == "__main__":
    unittest.main()

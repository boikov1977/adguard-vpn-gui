#!/usr/bin/python3
"""Maintenance-script checks for the .deb build.

Everything reads packaging/build-deb.sh, so no root and no dpkg are needed.
Run:  python3 -m unittest discover -s tests -v
"""
from __future__ import annotations

import re
import shutil
import subprocess
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
BUILD = ROOT / "packaging" / "build-deb.sh"
APP = ROOT / "agvpn" / "app.py"


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
        entry = "adguard-vpn-gui.desktop"
        self.assertIn(f'"{entry}"', APP.read_text(encoding="utf-8"))
        postrm = maintenance_script("postrm")
        self.assertIn("/home/*/.config/autostart", postrm)
        self.assertIn(f'"$dir/{entry}"', postrm)
        self.assertIn("rm -f", postrm)

    def test_postrm_cleanup_is_not_run_on_upgrade(self):
        postrm = maintenance_script("postrm")
        case = re.search(r'case "\$1" in\n(.*?)\n\s*esac', postrm, re.S)
        self.assertIsNotNone(case, "postrm must branch on the dpkg action")
        self.assertIn("remove|purge)", case.group(1))


if __name__ == "__main__":
    unittest.main()

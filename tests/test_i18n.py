#!/usr/bin/python3
"""i18n infrastructure tests.

No compiled catalogs are required: Russian is the source language and the
fallback must always return the original string.
"""
from __future__ import annotations

import ast
import os
import re
import sys
import tempfile
import unittest
from contextlib import contextmanager
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import i18n  # noqa: E402

LOCALE_FILES = (
    ROOT / "agvpn" / "locale" / "adguard-vpn-gui.pot",
    ROOT / "agvpn" / "locale" / "en" / "LC_MESSAGES" / "adguard-vpn-gui.po",
)


@contextmanager
def locale_env(**values):
    """Set locale variables and remove the ones not explicitly provided."""
    names = ("LANGUAGE", "LC_ALL", "LC_MESSAGES", "LANG", "AGVPN_GUI_LOCALEDIR")
    with mock.patch.dict(os.environ, values, clear=False):
        for name in names:
            if name not in values:
                os.environ.pop(name, None)
        yield


class NormalizationTests(unittest.TestCase):
    def test_encoding_and_modifier_are_removed(self):
        self.assertEqual(i18n.normalize_locale("en_US.UTF-8"), "en_US")
        self.assertEqual(i18n.normalize_locale("ru-RU@euro"), "ru_RU")
        self.assertEqual(i18n.normalize_locale("de_DE.utf8"), "de_DE")

    def test_plain_and_missing_values(self):
        self.assertEqual(i18n.normalize_locale("en"), "en")
        self.assertIsNone(i18n.normalize_locale("C"))
        self.assertIsNone(i18n.normalize_locale("POSIX"))
        self.assertIsNone(i18n.normalize_locale(""))
        self.assertIsNone(i18n.normalize_locale(None))

    def test_language_list_order_and_base_fallback(self):
        with locale_env(LANGUAGE="en:ru"):
            self.assertEqual(i18n.languages(), ("en", "ru"))

    def test_language_wins_over_locale_variables(self):
        with locale_env(LANGUAGE="fr", LC_ALL="de_DE.UTF-8", LANG="ru_RU.UTF-8"):
            self.assertEqual(i18n.languages(), ("fr", "de_DE", "de", "ru_RU", "ru"))

    def test_c_locale_means_no_translation(self):
        with locale_env(LC_ALL="C", LANG="C.UTF-8"):
            self.assertEqual(i18n.languages(), ())


class FallbackTests(unittest.TestCase):
    def test_missing_catalog_returns_msgid(self):
        with tempfile.TemporaryDirectory() as tmp, locale_env(
            AGVPN_GUI_LOCALEDIR=tmp, LANGUAGE="zz"
        ), mock.patch.object(i18n, "DOMAIN", "adguard-vpn-gui-test-missing"):
            self.assertEqual(i18n._("Привет, мир"), "Привет, мир")

    def test_missing_catalog_ngettext_fallback(self):
        with tempfile.TemporaryDirectory() as tmp, locale_env(
            AGVPN_GUI_LOCALEDIR=tmp, LANGUAGE="zz"
        ), mock.patch.object(i18n, "DOMAIN", "adguard-vpn-gui-test-missing"):
            self.assertEqual(i18n.ngettext("один сайт", "много сайтов", 1), "один сайт")
            self.assertEqual(i18n.ngettext("один сайт", "много сайтов", 2), "много сайтов")

    def test_localedir_without_mo_file_falls_back(self):
        with tempfile.TemporaryDirectory() as tmp:
            locale_dir = Path(tmp) / "zz" / "LC_MESSAGES"
            locale_dir.mkdir(parents=True)
            # An incomplete tree without the expected .mo must not break startup.
            (locale_dir / "adguard-vpn-gui.po").write_text("", encoding="utf-8")
            with locale_env(AGVPN_GUI_LOCALEDIR=tmp, LANGUAGE="zz"), \
                    mock.patch.object(i18n, "DOMAIN", "adguard-vpn-gui-test-missing"):
                self.assertEqual(i18n._("Строка"), "Строка")

    def test_localedirs_prefers_override(self):
        with tempfile.TemporaryDirectory() as tmp, locale_env(AGVPN_GUI_LOCALEDIR=tmp):
            dirs = i18n.localedirs()
            self.assertEqual(dirs[0], Path(tmp))
            self.assertIn(i18n.PKG_LOCALE_DIR, dirs)


class EnglishCatalogTests(unittest.TestCase):
    """The compiled English catalog must be used when English is requested."""

    MO_FILE = (
        ROOT / "agvpn" / "locale" / "en" / "LC_MESSAGES" / "adguard-vpn-gui.mo"
    )

    def setUp(self):
        self._saved_state = dict(i18n._state)
        i18n._state["key"] = None

    def tearDown(self):
        i18n._state.clear()
        i18n._state.update(self._saved_state)

    def test_english_singular_strings(self):
        self.assertTrue(self.MO_FILE.is_file(), "run msgfmt to build the English catalog")
        with locale_env(LANGUAGE="en", LC_ALL="en_US.UTF-8"):
            i18n._state["key"] = None
            self.assertEqual(i18n._("Подключиться"), "Connect")
            self.assertEqual(i18n._("Локации"), "Locations")
            self.assertEqual(i18n._("Отмена"), "Cancel")

    def test_english_plural_forms(self):
        with locale_env(LANGUAGE="en", LC_ALL="en_US.UTF-8"):
            i18n._state["key"] = None
            singular = i18n.ngettext("Доступна локация: {n}", "Доступно локаций: {n}", 1)
            plural = i18n.ngettext("Доступна локация: {n}", "Доступно локаций: {n}", 2)
            self.assertEqual(singular, "{n} location available")
            self.assertEqual(plural, "{n} locations available")
            self.assertNotEqual(singular, plural)

    def test_unknown_language_falls_back_to_russian(self):
        with locale_env(LANGUAGE="zz", LC_ALL="zz_ZZ.UTF-8"):
            i18n._state["key"] = None
            self.assertEqual(i18n._("Подключиться"), "Подключиться")
            self.assertEqual(
                i18n.ngettext("Доступна локация: {n}", "Доступно локаций: {n}", 2),
                "Доступно локаций: {n}",
            )


class SourceHygieneTests(unittest.TestCase):
    def test_no_fstring_inside_gettext_call(self):
        pattern = re.compile(r"_\s*\(\s*f(?:'''|\"\"\"|'|\")")
        offenders: list[str] = []
        for path in sorted((ROOT / "agvpn").glob("*.py")):
            text = path.read_text(encoding="utf-8")
            if pattern.search(text):
                offenders.append(path.name)
        self.assertEqual(offenders, [])

    def test_cli_parsers_do_not_call_gettext(self):
        source = (ROOT / "agvpn" / "cli.py").read_text(encoding="utf-8")
        tree = ast.parse(source)
        protected = {"_raise_for_output", "check_update"}
        offenders: list[str] = []
        for node in ast.walk(tree):
            if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef)):
                continue
            if not (node.name.startswith("parse_") or node.name in protected):
                continue
            for child in ast.walk(node):
                if not isinstance(child, ast.Call):
                    continue
                func = child.func
                if isinstance(func, ast.Name) and func.id in {"_", "ngettext"}:
                    offenders.append(f"{node.name}:{child.lineno}")
        self.assertEqual(offenders, [])

    def test_catalogs_exist_and_declare_utf8(self):
        for path in LOCALE_FILES:
            self.assertTrue(path.is_file(), path)
            text = path.read_text(encoding="utf-8")
            self.assertIn("charset=UTF-8", text)


if __name__ == "__main__":
    unittest.main()

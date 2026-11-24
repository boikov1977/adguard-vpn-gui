#!/usr/bin/python3
"""UnRKN blocklist tests (T-41).  No display and no real network are needed."""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import blocklist  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"
SAMPLE = (FIX / "ru_blocklist_sample.txt").read_text(encoding="utf-8")


class NormalizationTests(unittest.TestCase):
    def test_ru_rule_keeps_only_ru_for_brand(self):
        domains, raw = blocklist.parse_text(SAMPLE)
        self.assertEqual(raw, 17)
        self.assertIn("ozon.ru", domains)
        for dropped in ("ozon.by", "ozon.com", "ozon.de", "ozon.kz", "ozon.su", "ozon.tech"):
            self.assertNotIn(dropped, domains)
        self.assertIn("2gis.ru", domains)
        self.assertNotIn("2gis.ae", domains)
        self.assertNotIn("2gis.com", domains)

    def test_brand_without_ru_is_kept_as_is(self):
        domains, _raw = blocklist.parse_text(SAMPLE)
        for kept in ("icq.com", "icq.im", "icq.net", "telega.info", "telega.me"):
            self.assertIn(kept, domains)

    def test_duplicates_and_junk_are_removed(self):
        domains, raw = blocklist.parse_text(SAMPLE)
        self.assertEqual(domains.count("example.com"), 1)
        self.assertEqual(domains.count("prefixed.example"), 1)
        self.assertNotIn("not a domain", domains)
        self.assertNotIn("https://bad.example/path", domains)
        self.assertEqual(len(domains), 9)

    def test_empty_text(self):
        self.assertEqual(blocklist.parse_text(""), ([], 0))
        self.assertEqual(blocklist.parse_text("\n# only comments\n"), ([], 0))

    def test_uppercase_and_trailing_dot_are_normalized(self):
        domains, _raw = blocklist.parse_text("OZON.RU.\nOzon.By\n")
        self.assertEqual(domains, ["ozon.ru"])

    def test_punycode_domain_is_kept(self):
        domains, raw = blocklist.parse_text("nalog.xn--p1ai\n")
        self.assertEqual(domains, ["nalog.xn--p1ai"])
        self.assertEqual(raw, 1)

    def test_wildcard_is_treated_like_its_base_domain(self):
        # ``*.ozon.com`` is subsumed by any rule covering ``ozon.com``, so the
        # .ru rule drops it exactly like the bare domain.
        self.assertEqual(
            blocklist.normalize_domains(
                ["ozon.ru", "ozon.by", "*.ozon.com", "*.ozon.by"]
            ),
            ["ozon.ru"],
        )

    def test_wildcard_survives_when_brand_has_no_ru(self):
        # A bare TLD is not a valid host (HOST_RE), so the widest real rule is
        # another zone of the same brand: without .ru the whole group is kept.
        self.assertEqual(
            blocklist.normalize_domains(["*.ozon.com", "ozon.by"]),
            ["*.ozon.com", "ozon.by"],
        )
        self.assertEqual(
            blocklist.normalize_domains(["*.ozon.com", "example.com"]),
            ["*.ozon.com", "example.com"],
        )


class CacheTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cache" / "ru-blocklist.json"

    def tearDown(self):
        self.tmp.cleanup()

    def _data(self) -> blocklist.Blocklist:
        domains, raw = blocklist.parse_text(SAMPLE)
        return blocklist.Blocklist(tuple(domains), raw, datetime(2026, 11, 22, 4, 50))

    def test_roundtrip(self):
        data = self._data()
        blocklist.save_cache(data, self.path)
        loaded = blocklist.load_cache(self.path)
        self.assertIsNotNone(loaded)
        self.assertEqual(loaded.domains, data.domains)
        self.assertEqual(loaded.raw_count, data.raw_count)
        self.assertEqual(loaded.fetched_at, data.fetched_at)
        self.assertEqual(loaded.source, blocklist.SOURCE_URL)

    def test_json_shape_and_permissions(self):
        blocklist.save_cache(self._data(), self.path)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1)
        self.assertEqual(raw["raw_count"], 17)
        self.assertIn("ozon.ru", raw["domains"])
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)

    def test_missing_and_corrupt_cache(self):
        self.assertIsNone(blocklist.load_cache(self.path))
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{nope", encoding="utf-8")
        self.assertIsNone(blocklist.load_cache(self.path))

    def test_cache_without_domains_is_ignored(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({"version": 1, "domains": []}), encoding="utf-8")
        self.assertIsNone(blocklist.load_cache(self.path))

    def test_cache_is_renormalized_on_load(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "version": 1,
            "raw_count": 3,
            "domains": ["ozon.by", "ozon.ru", "icq.com"],
        }), encoding="utf-8")
        loaded = blocklist.load_cache(self.path)
        self.assertEqual(loaded.domains, ("ozon.ru", "icq.com"))

    def test_fetched_text(self):
        self.assertEqual(blocklist.fetched_text(datetime(2026, 11, 22, 4, 50)), "22.11 в 04:50")
        self.assertEqual(blocklist.fetched_text(None), "")


class FetchTests(unittest.TestCase):
    def test_fetch_from_file_url(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "list.txt"
            source.write_text(SAMPLE, encoding="utf-8")
            data = blocklist.fetch(source.as_uri())
        self.assertIn("ozon.ru", data.domains)
        self.assertEqual(data.raw_count, 17)

    def test_fetch_empty_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            source = Path(tmp) / "empty.txt"
            source.write_text("# nothing\n", encoding="utf-8")
            with self.assertRaises(blocklist.BlocklistError):
                blocklist.fetch(source.as_uri())

    def test_fetch_missing_file_raises(self):
        with tempfile.TemporaryDirectory() as tmp:
            missing = Path(tmp) / "missing.txt"
            with self.assertRaises(blocklist.BlocklistError):
                blocklist.fetch(missing.as_uri())


if __name__ == "__main__":
    unittest.main()

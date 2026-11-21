#!/usr/bin/python3
"""Endpoint history tests (T-40).  Run: python3 -m unittest discover -s tests -v

Only temporary files are touched; no display and no network are needed.
"""
from __future__ import annotations

import json
import sys
import tempfile
import unittest
from datetime import datetime, timedelta
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import pings, tunnel  # noqa: E402
from agvpn.cli import Location  # noqa: E402

HELSINKI = Location(iso="FI", country="Finland", city="Helsinki", ping=17)
MEASURED = datetime(2026, 11, 22, 0, 24, 34)


def endpoint(ping: int = 231, measured_at: datetime | None = MEASURED) -> tunnel.Endpoint:
    return tunnel.Endpoint(
        name="cache-25.smart-pixl.net",
        address="201.8.95.149:443",
        relay="none",
        ping=ping,
        measured_at=measured_at,
    )


class LocationKeyTests(unittest.TestCase):
    def test_key_is_case_and_space_insensitive(self):
        self.assertEqual(
            pings.location_key("fi", "  Helsinki "),
            pings.location_key("FI", "helsinki"),
        )

    def test_key_without_iso_is_rejected(self):
        self.assertIsNone(pings.location_key("", "Helsinki"))
        self.assertIsNone(pings.location_key(None, "Helsinki"))

    def test_key_requires_city(self):
        self.assertIsNone(pings.location_key("FI", "  "))
        self.assertIsNone(pings.location_key(None, None))


class HistoryTextTests(unittest.TestCase):
    def test_text_has_value_and_date(self):
        self.assertEqual(
            pings.history_text(231, datetime(2026, 11, 22, 0, 24)),
            "231 мс · 22.11",
        )

    def test_text_without_date_is_still_a_measurement(self):
        self.assertEqual(pings.history_text(231, None), "231 мс")

    def test_unknown_ping_is_dash(self):
        self.assertEqual(pings.history_text(None, MEASURED), "—")


class EndpointHistoryTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / "cache" / "endpoints.json"
        self.history = pings.EndpointHistory(self.path)

    def tearDown(self):
        self.tmp.cleanup()

    def test_record_and_get(self):
        self.assertTrue(self.history.record(HELSINKI, endpoint(), now=MEASURED))
        stored = self.history.get(HELSINKI, now=MEASURED + timedelta(hours=1))
        self.assertIsNotNone(stored)
        self.assertEqual(stored.ping, 231)
        self.assertEqual(stored.endpoint, "cache-25.smart-pixl.net")
        self.assertEqual(stored.address, "201.8.95.149:443")
        self.assertEqual(stored.measured_at, MEASURED)

    def test_record_persists_between_instances(self):
        self.history.record(HELSINKI, endpoint(), now=MEASURED)
        again = pings.EndpointHistory(self.path)
        stored = again.get(HELSINKI, now=MEASURED)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.ping, 231)

    def test_json_shape(self):
        self.history.record(HELSINKI, endpoint(), now=MEASURED)
        raw = json.loads(self.path.read_text(encoding="utf-8"))
        self.assertEqual(raw["version"], 1)
        item = raw["locations"]["FI/helsinki"]
        self.assertEqual(item["endpoint"], "cache-25.smart-pixl.net")
        self.assertEqual(item["address"], "201.8.95.149:443")
        self.assertEqual(item["ping"], 231)
        self.assertEqual(item["measured_at"], "2026-11-22T00:24:34")

    def test_duplicate_measurement_is_not_rewritten(self):
        self.assertTrue(self.history.record(HELSINKI, endpoint(), now=MEASURED))
        self.assertFalse(self.history.record(HELSINKI, endpoint(), now=MEASURED))
        # A newer measurement with another ping replaces the old one.
        newer = MEASURED + timedelta(hours=2)
        self.assertTrue(self.history.record(HELSINKI, endpoint(187, newer), now=newer))
        self.assertEqual(self.history.get(HELSINKI, now=newer).ping, 187)

    def test_stale_measurement_is_not_returned(self):
        self.history.record(HELSINKI, endpoint(), now=MEASURED)
        fresh = self.history.get(HELSINKI, now=MEASURED + pings.MAX_AGE)
        stale = self.history.get(HELSINKI, now=MEASURED + pings.MAX_AGE + timedelta(seconds=1))
        self.assertIsNotNone(fresh)
        self.assertIsNone(stale)

    def test_record_without_log_timestamp_uses_now(self):
        now = datetime(2026, 11, 22, 12, 0, 0)
        self.history.record(HELSINKI, endpoint(measured_at=None), now=now)
        stored = self.history.get(HELSINKI, now=now)
        self.assertIsNotNone(stored)
        self.assertEqual(stored.measured_at, now)

    def test_record_without_timestamp_does_not_rewrite_every_poll(self):
        first = datetime(2026, 11, 22, 12, 0, 0)
        later = first + timedelta(seconds=5)
        self.assertTrue(self.history.record(HELSINKI, endpoint(measured_at=None), now=first))
        self.assertFalse(self.history.record(HELSINKI, endpoint(measured_at=None), now=later))
        self.assertEqual(self.history.get(HELSINKI, now=later).measured_at, first)

    def test_unknown_location_is_not_cached(self):
        empty = Location(iso="", country="", city="", ping=None)
        self.assertFalse(self.history.record(empty, endpoint(), now=MEASURED))
        self.assertFalse(self.path.exists())

    def test_corrupt_file_is_treated_as_empty(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text("{not json", encoding="utf-8")
        history = pings.EndpointHistory(self.path)
        self.assertIsNone(history.get(HELSINKI, now=MEASURED))
        self.assertTrue(history.record(HELSINKI, endpoint(), now=MEASURED))

    def test_bad_entries_are_skipped(self):
        self.path.parent.mkdir(parents=True)
        self.path.write_text(json.dumps({
            "version": 1,
            "locations": {
                "FI/helsinki": {"ping": "oops", "measured_at": "2026-11-22T00:00:00"},
                "FI/turku": {"ping": 42, "measured_at": "2026-11-22T00:00:00",
                             "endpoint": "x", "address": "y"},
            },
        }), encoding="utf-8")
        history = pings.EndpointHistory(self.path)
        self.assertIsNone(history.get(HELSINKI, now=MEASURED))
        turku = Location(iso="FI", country="Finland", city="Turku", ping=None)
        self.assertEqual(history.get(turku, now=MEASURED).ping, 42)

    def test_cache_file_permissions_are_private(self):
        self.history.record(HELSINKI, endpoint(), now=MEASURED)
        self.assertEqual(self.path.stat().st_mode & 0o777, 0o600)


if __name__ == "__main__":
    unittest.main()

#!/usr/bin/python3
"""Location matching tests.  Run:  python3 -m unittest discover -s tests -v

No GTK widgets are created, so these run headless.
"""
from __future__ import annotations

import sys
import unittest
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn.cli import Location  # noqa: E402
from agvpn.locations import match_location, same_location  # noqa: E402


def loc(iso: str, country: str, city: str, ping: int | None = 20) -> Location:
    return Location(iso=iso, country=country, city=city, ping=ping)


HELSINKI = loc("FI", "Finland", "Helsinki", 22)
ESPOO = loc("FI", "Finland", "Espoo", 24)
YORK = loc("GB", "United Kingdom", "York", 40)
NEW_YORK = loc("US", "United States", "New York", 90)
MOSCOW = loc("RU", "Russia", "Moscow", 30)

LOCATIONS = [HELSINKI, ESPOO, YORK, NEW_YORK, MOSCOW]


class MatchLocationTests(unittest.TestCase):
    def test_exact_city_beats_substring(self):
        self.assertIs(match_location("New York", LOCATIONS), NEW_YORK)

    def test_exact_city_is_case_insensitive(self):
        self.assertIs(match_location("HELSINKI", LOCATIONS), HELSINKI)

    def test_city_with_country(self):
        self.assertIs(match_location("Helsinki, Finland", LOCATIONS), HELSINKI)

    def test_longest_substring_wins(self):
        self.assertIs(match_location("City of New York", LOCATIONS), NEW_YORK)

    def test_exact_country_returns_first_location(self):
        self.assertIs(match_location("Finland", LOCATIONS), HELSINKI)

    def test_country_prefers_last_location(self):
        last = {"iso": "FI", "country": "Finland", "city": "Espoo", "ping": 24}
        matched = match_location("Finland", LOCATIONS, last)
        self.assertIsNotNone(matched)
        self.assertEqual(matched.city, "Espoo")
        self.assertEqual(matched.iso, "FI")

    def test_iso_code_matches_country(self):
        self.assertIs(match_location("FI", LOCATIONS), HELSINKI)

    def test_empty_status_returns_last(self):
        last = {"iso": "FI", "country": "Finland", "city": "Helsinki", "ping": 22}
        matched = match_location("", LOCATIONS, last)
        self.assertIsNotNone(matched)
        self.assertEqual(matched.city, "Helsinki")
        self.assertEqual(matched.iso, "FI")

    def test_empty_status_without_last_is_none(self):
        self.assertIsNone(match_location("", LOCATIONS, None))

    def test_unknown_city_is_none(self):
        self.assertIsNone(match_location("Atlantis", LOCATIONS))

    def test_unknown_city_with_last_is_not_guessed(self):
        last = {"iso": "FI", "country": "Finland", "city": "Helsinki", "ping": 22}
        self.assertIsNone(match_location("Atlantis", LOCATIONS, last))

    def test_york_does_not_capture_new_york(self):
        self.assertIs(match_location("New York", [YORK, NEW_YORK]), NEW_YORK)
        self.assertIs(match_location("York", [YORK, NEW_YORK]), YORK)


class SameLocationTests(unittest.TestCase):
    def test_same_city_and_iso(self):
        self.assertTrue(same_location(
            loc("FI", "Finland", "Helsinki"), loc("fi", "Finland", "HELSINKI")
        ))

    def test_same_city_different_iso(self):
        self.assertFalse(same_location(
            loc("FI", "Finland", "Helsinki"), loc("SE", "Sweden", "Helsinki")
        ))

    def test_missing_iso_is_tolerated(self):
        self.assertTrue(same_location(
            loc("", "", "Helsinki"), loc("FI", "Finland", "Helsinki")
        ))

    def test_none_is_never_current(self):
        self.assertFalse(same_location(None, HELSINKI))
        self.assertFalse(same_location(HELSINKI, None))


if __name__ == "__main__":
    unittest.main()

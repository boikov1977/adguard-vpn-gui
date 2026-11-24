#!/usr/bin/python3
"""Remembering the location reported by ``adguardvpn-cli status``.

``status.location`` is parsed by ``AdGuardCLI.parse_status`` as
"City, Country" (or just "City" when the trailing "in … mode" note is
stripped).  Matching must be exact per comma-separated part: a substring
match would remember "York" for "New York, United States".

No display is needed: the method runs on a duck-typed stub.
"""
from __future__ import annotations

import sys
import types
import unittest
from pathlib import Path
from unittest import mock

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn.app import MainWindow  # noqa: E402
from agvpn.cli import Location, VpnStatus  # noqa: E402


def loc(iso: str, city: str, country: str, ping: int | None = 20) -> Location:
    return Location(iso=iso, country=country, city=city, ping=ping)


class _Window:
    def __init__(self, locations: list[Location], last: dict | None = None) -> None:
        self.locations = locations
        self.store = types.SimpleNamespace(
            last_location=last,
            set_last_location=mock.Mock(),
        )

    def remember(self, location: str) -> mock.Mock:
        MainWindow._maybe_remember_status_location(
            self, VpnStatus(state="connected", location=location))
        return self.store.set_last_location


class StatusLocationMatchTests(unittest.TestCase):
    def test_substring_city_does_not_win(self):
        # Regression: "York" was a substring of "New York, United States" and
        # was remembered instead of the city the CLI actually reported.
        win = _Window([
            loc("US", "York", "United States"),
            loc("US", "New York", "United States", 42),
        ])
        call = win.remember("New York, United States")
        call.assert_called_once_with("US", "United States", "New York", 42)

    def test_city_and_country_are_matched_exactly(self):
        win = _Window([
            loc("FI", "Helsinki", "Finland"),
            loc("SE", "Stockholm", "Sweden"),
        ])
        call = win.remember("Helsinki, Finland")
        call.assert_called_once_with("FI", "Finland", "Helsinki", 20)

    def test_country_must_match_when_both_are_listed(self):
        # Two cities share a name; only the country part disambiguates them.
        win = _Window([
            loc("DE", "Springfield", "Germany"),
            loc("US", "Springfield", "United States"),
        ])
        call = win.remember("Springfield, United States")
        call.assert_called_once_with("US", "United States", "Springfield", 20)

    def test_unknown_part_is_not_remembered(self):
        win = _Window([loc("FI", "Helsinki", "Finland")])
        call = win.remember("Helsinki, Sweden")
        call.assert_not_called()

    def test_last_city_matches_by_exact_part(self):
        win = _Window(
            [loc("US", "New York", "United States")],
            last={"iso": "US", "country": "United States", "city": "New York"},
        )
        call = win.remember("New York, United States")
        call.assert_not_called()

    def test_last_city_substring_is_not_already_remembered(self):
        win = _Window(
            [loc("US", "New York", "United States")],
            last={"iso": "US", "country": "United States", "city": "York"},
        )
        call = win.remember("New York, United States")
        call.assert_called_once_with("US", "United States", "New York", 20)

    def test_single_token_location_needs_only_the_city(self):
        # "Connected to Amsterdam in TUN mode" is parsed down to "Amsterdam".
        win = _Window([loc("NL", "Amsterdam", "Netherlands")])
        call = win.remember("Amsterdam")
        call.assert_called_once_with("NL", "Netherlands", "Amsterdam", 20)

    def test_extra_spaces_around_parts_are_ignored(self):
        win = _Window([loc("FI", "Helsinki", "Finland")])
        call = win.remember("  Helsinki ,  Finland ")
        call.assert_called_once_with("FI", "Finland", "Helsinki", 20)

    def test_empty_location_is_ignored(self):
        win = _Window([loc("FI", "Helsinki", "Finland")])
        call = win.remember("")
        call.assert_not_called()


if __name__ == "__main__":
    unittest.main()

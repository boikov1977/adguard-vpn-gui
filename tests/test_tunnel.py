#!/usr/bin/python3
"""tunnel.log parser tests.  Run:  python3 -m unittest discover -s tests -v

These tests only touch temporary files and never need a display.
"""
from __future__ import annotations

import sys
import tempfile
import unittest
from datetime import datetime
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from agvpn import tunnel, util  # noqa: E402

FIX = Path(__file__).resolve().parent / "fixtures"

REAL_LINE = (
    "22.11.2026 00:24:34.822055 INFO  [112410] VPNCORE pinger_handler: "
    "[0] Using endpoint: name=cache-25.smart-pixl.net, "
    "address=201.8.95.149:443, relay=none, ping=231ms"
)


def read(name: str) -> str:
    return (FIX / name).read_text(encoding="utf-8")


class TunnelLogParsingTests(unittest.TestCase):
    def test_real_line_fields(self):
        endpoint = tunnel.read_last_endpoint(FIX / "tunnel_log_single.txt")
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.name, "cache-25.smart-pixl.net")
        self.assertEqual(endpoint.address, "201.8.95.149:443")
        self.assertEqual(endpoint.relay, "none")
        self.assertEqual(endpoint.ping, 231)
        self.assertEqual(endpoint.measured_at, datetime(2026, 11, 22, 0, 24, 34, 822055))

    def test_multiple_lines_use_last_match(self):
        endpoint = tunnel.read_last_endpoint(FIX / "tunnel_log_multiple.txt")
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.name, "cache-40.example.net")
        self.assertEqual(endpoint.address, "198.51.100.7:443")
        self.assertEqual(endpoint.relay, "eu-relay-1")
        self.assertEqual(endpoint.ping, 187)

    def test_parse_line_without_timestamp_keeps_ping(self):
        endpoint = tunnel.parse_line(
            "pinger_handler: [0] Using endpoint: name=n.example, "
            "address=203.0.113.9:443, relay=none, ping=42ms"
        )
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.ping, 42)
        self.assertIsNone(endpoint.measured_at)

    def test_zero_ping_is_valid(self):
        endpoint = tunnel.parse_line(
            "Using endpoint: name=local, address=127.0.0.1:443, "
            "relay=none, ping=0ms"
        )
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.ping, 0)

    def test_large_ping_is_valid(self):
        endpoint = tunnel.parse_line(
            "Using endpoint: name=far, address=203.0.113.1:443, "
            "relay=relay-a, ping=9876ms"
        )
        self.assertIsNotNone(endpoint)
        self.assertEqual(endpoint.ping, 9876)

    def test_broken_line_is_none(self):
        broken = [
            "",
            "Using endpoint:",
            "Using endpoint: name=x, address=1.2.3.4:443, relay=none",
            "Using endpoint: name=x, address=1.2.3.4:443, relay=none, ping=ms",
            "Using endpoint: name=x, ping=12ms",
            "nonsense without endpoint",
        ]
        with tempfile.TemporaryDirectory() as tmp:
            for index, line in enumerate(broken):
                with self.subTest(line=line):
                    path = Path(tmp) / f"broken{index}.log"
                    path.write_text(line + "\n", encoding="utf-8")
                    self.assertIsNone(tunnel.parse_line(line))
                    self.assertIsNone(tunnel.read_last_endpoint(path))

    def test_empty_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            path.write_text("", encoding="utf-8")
            self.assertIsNone(tunnel.read_last_endpoint(path))

    def test_missing_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(tunnel.read_last_endpoint(Path(tmp) / "nope.log"))

    def test_directory_instead_of_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            self.assertIsNone(tunnel.read_last_endpoint(Path(tmp)))

    def test_reads_only_tail_and_ignores_old_endpoint(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            old = (
                "22.11.2026 00:00:00.000001 INFO [1] VPNCORE pinger_handler: "
                "[0] Using endpoint: name=old.example, address=203.0.113.2:443, "
                "relay=none, ping=999ms\n"
            )
            filler = ("22.11.2026 00:00:01.000000 INFO [1] VPNCORE noise: x\n" * 4000)
            new = (
                "22.11.2026 00:24:34.822055 INFO [2] VPNCORE pinger_handler: "
                "[0] Using endpoint: name=new.example, address=203.0.113.3:443, "
                "relay=none, ping=15ms\n"
            )
            path.write_text(old + filler + new, encoding="utf-8")
            self.assertGreater(path.stat().st_size, tunnel.READ_TAIL_BYTES)
            endpoint = tunnel.read_last_endpoint(path)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.name, "new.example")
            self.assertEqual(endpoint.ping, 15)

    def test_partial_first_line_in_tail_is_dropped(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            # Make the file larger than the tail window and put the valid
            # record exactly at the end; the truncated line in the middle must
            # not be parsed as a whole record.
            payload = "x" * (tunnel.READ_TAIL_BYTES + 100)
            path.write_text(payload + "\n" + REAL_LINE + "\n", encoding="utf-8")
            endpoint = tunnel.read_last_endpoint(path)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.ping, 231)

    def test_tail_without_newline_is_returned_as_is(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            # The 64 KB window has no newline at all.  Dropping everything
            # would hide the endpoint; the text must be kept as-is.
            path.write_text("x" * (tunnel.READ_TAIL_BYTES + 10) + REAL_LINE,
                            encoding="utf-8")
            self.assertGreater(path.stat().st_size, tunnel.READ_TAIL_BYTES)
            endpoint = tunnel.read_last_endpoint(path)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.ping, 231)

    def test_tail_starting_on_line_boundary_keeps_first_line(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            # The window is exactly 64 KB and starts right after a newline,
            # so the first line is complete and must not be dropped.
            window = REAL_LINE + "\n" + "z" * (tunnel.READ_TAIL_BYTES - len(REAL_LINE) - 1)
            self.assertEqual(len(window), tunnel.READ_TAIL_BYTES)
            path.write_text("prefix\n" + window, encoding="utf-8")
            endpoint = tunnel.read_last_endpoint(path)
            self.assertIsNotNone(endpoint)
            self.assertEqual(endpoint.ping, 231)

    def test_cache_missing_file_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            cache = tunnel.EndpointCache(Path(tmp) / "nope.log")
            self.assertIsNone(cache.get())
            self.assertIsNone(cache.get())

    def test_cache_reuses_and_refreshes(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "tunnel.log"
            path.write_text(read("tunnel_log_single.txt"), encoding="utf-8")
            cache = tunnel.EndpointCache(path)
            first = cache.get()
            self.assertIsNotNone(first)
            self.assertEqual(first.ping, 231)
            # Same (mtime, size) -> same object, no re-read.
            self.assertIs(cache.get(), first)
            path.write_text(read("tunnel_log_multiple.txt"), encoding="utf-8")
            second = cache.get()
            self.assertIsNotNone(second)
            self.assertEqual(second.name, "cache-40.example.net")
            cache.invalidate()
            self.assertEqual(cache.get().name, "cache-40.example.net")


class PingFormattingTests(unittest.TestCase):
    """Estimates must be visibly different from measured values."""

    def test_estimate_is_marked(self):
        self.assertEqual(util.human_estimate(17), "≈17 мс")
        self.assertEqual(util.human_estimate(0), "≈0 мс")
        self.assertEqual(util.human_estimate(None), "—")
        self.assertEqual(util.human_estimate(9999), "—")

    def test_measurement_has_no_estimate_mark(self):
        self.assertEqual(util.human_ping(231), "231 мс")
        self.assertEqual(util.human_ping(None), "—")
        self.assertNotIn("≈", util.human_ping(231))

    def test_tooltip_explains_estimate(self):
        self.assertIn("Оценка AdGuard", util.ESTIMATE_TOOLTIP)
        self.assertIn("не измерение", util.ESTIMATE_TOOLTIP.lower())

    def test_measured_at_text(self):
        stamp = datetime(2026, 11, 22, 0, 24, 34, 822055)
        self.assertEqual(util.measured_at_text(stamp), "22.11 00:24:34")
        self.assertEqual(util.measured_at_text(None), "")


if __name__ == "__main__":
    unittest.main()
